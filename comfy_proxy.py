#!/usr/bin/env python3
"""
Tiny local proxy so the Ideogrammar editor (a browser page) can drive ComfyUI
without ComfyUI needing --enable-cors-header.

Why this exists:
  CORS is a *browser* rule. Your Python ComfyUIClient never hits it because it
  talks server-to-server. This proxy does the same thing: the browser talks to
  THIS server (same origin => no CORS), and this server forwards everything to
  ComfyUI exactly like a Python client would.

What it does:
  - Serves index.html at  http://<listen>/  (so the page is same-origin).
  - Transparently forwards every other HTTP request to ComfyUI
    (/prompt, /history, /view, /upload/image, /interrupt, /queue, ...).
  - Relays the /ws WebSocket to ComfyUI (raw byte relay -> live progress works).
  - Adds permissive CORS headers too, so it also works if you open the page as
    a file:// and just point its Server URL at this proxy.

Usage:
  python comfy_proxy.py
  python comfy_proxy.py --comfy http://127.0.0.1:8188 --port 8189
  # then open http://localhost:8189/ and set the editor's Server URL to
  #   http://localhost:8189   (or leave blank when served from here)

Stdlib only. Python 3.8+.
"""

import argparse
import base64
import hashlib
import http.client
import io
import json
import mimetypes
import os
import re
import select
import socket
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote, urlencode

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

ARGS = None  # set in main()


def comfy_host_port():
    u = urlsplit(ARGS.comfy)
    return u.hostname, (u.port or (443 if u.scheme == "https" else 80))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ComfyProxy/1.0"

    # ---- logging: quiet but keep errors ----
    def log_message(self, fmt, *a):
        if ARGS.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))

    # ---- helpers ----
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _serve_index(self):
        try:
            with open(ARGS.html, "rb") as f:
                body = f.read()
        except OSError as e:
            self.send_error(500, "Cannot read %s: %s" % (ARGS.html, e))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _is_ws(self):
        up = self.headers.get("Upgrade", "")
        return up.lower() == "websocket"

    def _local_file(self, path):
        # Map a request path to an existing file next to index.html (for vendored
        # assets like vendor/gridstack-all.js). Returns None if no safe match.
        rel = unquote(path.split("?", 1)[0]).lstrip("/")
        if not rel:
            return None
        base = os.path.dirname(os.path.abspath(ARGS.html))
        cand = os.path.normpath(os.path.join(base, rel))
        if cand != base and not cand.startswith(base + os.sep):
            return None  # path traversal guard
        return cand if os.path.isfile(cand) else None

    def _json_error(self, code, msg):
        body = json.dumps({"error": msg}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _fetch_comfy_view(self, src):
        host, port = comfy_host_port()
        q = urlencode({
            "filename": src.get("filename", ""),
            "subfolder": src.get("subfolder", ""),
            "type": src.get("type", "output"),
        })
        try:
            conn = http.client.HTTPConnection(host, port, timeout=ARGS.timeout)
            conn.request("GET", "/view?" + q)
            r = conn.getresponse()
            data = r.read()
            conn.close()
            return data if r.status == 200 else None
        except Exception:
            return None

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    # ---- gallery recovery: list ComfyUI's output/ folder from disk ----
    # Survives a ComfyUI restart (unlike /history, which is in-memory only).
    # Requires --output-dir to point at a folder this proxy can read.
    _IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")

    def _png_prompt_graph(self, path):
        # Pull ComfyUI's embedded "prompt" (API prompt graph) from a PNG's text
        # chunks, without decoding pixels. Returns the parsed graph dict or None.
        try:
            with open(path, "rb") as f:
                if f.read(8) != b"\x89PNG\r\n\x1a\n":
                    return None
                texts = {}
                while True:
                    head = f.read(8)
                    if len(head) < 8:
                        break
                    length = int.from_bytes(head[:4], "big")
                    ctype = head[4:8]
                    if ctype in (b"IDAT", b"IEND"):
                        break  # all metadata sits before the pixel data
                    if ctype in (b"tEXt", b"zTXt", b"iTXt"):
                        data = f.read(length)
                        f.read(4)  # skip CRC
                        kw, val = self._parse_png_text(ctype, data)
                        if kw is not None:
                            texts[kw] = val
                    else:
                        f.seek(length + 4, io.SEEK_CUR)  # skip data + CRC
                raw = texts.get("prompt")
                if not raw:
                    return None
                return json.loads(raw)
        except Exception:
            return None

    @staticmethod
    def _parse_png_text(ctype, data):
        try:
            if ctype == b"tEXt":
                kw, _, txt = data.partition(b"\x00")
                return kw.decode("latin-1"), txt.decode("latin-1")
            if ctype == b"zTXt":
                kw, _, rest = data.partition(b"\x00")
                # rest = method(1 byte) + zlib-compressed text
                return kw.decode("latin-1"), zlib.decompress(rest[1:]).decode("latin-1")
            if ctype == b"iTXt":
                kw, _, rest = data.partition(b"\x00")
                comp_flag = rest[0:1]
                rest = rest[2:]                          # drop compression flag + method
                _, _, rest = rest.partition(b"\x00")     # drop language tag
                _, _, txt = rest.partition(b"\x00")      # drop translated keyword
                if comp_flag == b"\x01":
                    txt = zlib.decompress(txt)
                return kw.decode("latin-1"), txt.decode("utf-8", "replace")
        except Exception:
            pass
        return None, None

    def _list_outputs(self):
        root = ARGS.output_dir
        if not root or not os.path.isdir(root):
            self._send_json({
                "available": False,
                "reason": "This proxy has no --output-dir set (or it isn't a readable folder). "
                          "Restart it with --output-dir pointing at ComfyUI's output/ directory.",
            })
            return
        from urllib.parse import parse_qs
        q = parse_qs(urlsplit(self.path).query)
        prefix = (q.get("prefix", [""])[0] or "")
        try:
            limit = max(1, min(5000, int(q.get("limit", ["3000"])[0])))
        except ValueError:
            limit = 3000
        # Cheap pass: collect candidate files + mtime (no PNG parsing yet).
        cands = []
        scanned = 0
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.lower().endswith(self._IMG_EXT):
                    continue
                if prefix and not name.startswith(prefix):
                    continue
                scanned += 1
                if scanned > 40000:
                    break
                fp = os.path.join(dirpath, name)
                try:
                    mtime = os.path.getmtime(fp)
                except OSError:
                    continue
                sub = os.path.relpath(dirpath, root)
                sub = "" if sub == "." else sub.replace(os.sep, "/")
                cands.append((mtime, fp, name, sub))
            if scanned > 40000:
                break
        cands.sort(key=lambda c: c[0], reverse=True)
        cands = cands[:limit]
        files_out = []
        for mtime, fp, name, sub in cands:
            entry = {"filename": name, "subfolder": sub, "type": "output", "mtime": int(mtime * 1000)}
            if name.lower().endswith(".png"):
                g = self._png_prompt_graph(fp)
                if g is not None:
                    entry["prompt"] = g
            files_out.append(entry)
        self._send_json({"available": True, "files": files_out, "truncated": scanned > limit})

    def _vectorize(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw or b"{}")
        except Exception:
            self._json_error(400, "Invalid JSON body")
            return
        try:
            import PIL  # noqa: F401
            import vtracer  # noqa: F401
        except Exception as e:
            self._json_error(501, "Vectorizer dependencies missing. On the host running comfy_proxy.py: pip install --user vtracer pillow, then restart the proxy. [%s]" % e)
            return
        img_bytes = self._fetch_comfy_view(req.get("src") or {})
        if img_bytes is None:
            self._json_error(502, "Could not fetch the source image from ComfyUI")
            return
        try:
            svg, stats = vectorize_image(img_bytes, req.get("elements") or [], req.get("options") or {})
        except Exception as e:
            self._json_error(500, "Vectorize failed: %s" % e)
            return
        body = json.dumps({"svg": svg, "stats": stats}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    # ---- reference-image cache (for the editor's before/after compare slider) ----
    # The editor uploads the image a setup was generated from; we store it
    # content-addressed and hand back a short key, so the browser persists only
    # the key (not a base64 blob) and the compare survives reloads / the 300-entry
    # history cap. Files are tiny downscaled JPEGs.
    def _ref_dir(self):
        d = ARGS.ref_dir
        if not d:
            return None
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            return None

    def _store_refimg(self):
        d = self._ref_dir()
        if not d:
            self._json_error(500, "Reference-image cache directory is not writable")
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > 32 * 1024 * 1024:
            self._json_error(400, "Empty or oversized image (max 32 MB)")
            return
        data = self.rfile.read(length)
        ctype = (self.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip().lower()
        ext = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(ctype, ".jpg")
        key = hashlib.sha256(data).hexdigest()[:32] + ext
        path = os.path.join(d, key)
        if not os.path.exists(path):
            try:
                with open(path, "wb") as f:
                    f.write(data)
            except Exception as e:
                self._json_error(500, "Could not store image: %s" % e)
                return
        self._send_json({"key": key})

    def _serve_refimg(self, key):
        d = ARGS.ref_dir
        key = unquote(key or "")
        if not d or not re.fullmatch(r"[A-Fa-f0-9]{1,64}\.(jpg|png|webp)", key):
            self.send_error(404)
            return
        path = os.path.join(d, key)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, fpath):
        ctype = mimetypes.guess_type(fpath)[0] or "application/octet-stream"
        try:
            with open(fpath, "rb") as f:
                body = f.read()
        except OSError as e:
            self.send_error(500, "Cannot read %s: %s" % (fpath, e))
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    # ---- HTTP verbs ----
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if self._is_ws():
            self._relay_websocket()
            return
        if path == "/ideogrammar/outputs":
            self._list_outputs()
            return
        if path.startswith("/ideogrammar/refimg/"):
            self._serve_refimg(path[len("/ideogrammar/refimg/"):])
            return
        if path in ("/", "/index.html"):
            self._serve_index()
            return
        f = self._local_file(path)
        if f:
            self._serve_file(f)
            return
        self._proxy()

    def do_POST(self):
        p = self.path.split("?", 1)[0]
        if p == "/vectorize":
            self._vectorize()
            return
        if p == "/ideogrammar/refimg":
            self._store_refimg()
            return
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    # ---- transparent HTTP proxy to ComfyUI ----
    def _proxy(self):
        host, port = comfy_host_port()
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None

        fwd = {}
        for k, v in self.headers.items():
            if k.lower() in HOP_BY_HOP or k.lower() == "host":
                continue
            fwd[k] = v
        fwd["Host"] = "%s:%s" % (host, port)

        try:
            conn = http.client.HTTPConnection(host, port, timeout=ARGS.timeout)
            conn.request(self.command, self.path, body=body, headers=fwd)
            resp = conn.getresponse()
            data = resp.read()
        except Exception as e:
            self.send_error(502, "Upstream error: %s" % e)
            return

        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in HOP_BY_HOP or k.lower() in ("content-length", "access-control-allow-origin"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        conn.close()

    # ---- WebSocket relay (raw byte bridge after two independent handshakes) ----
    def _relay_websocket(self):
        # 1) Complete the handshake with the browser.
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self.send_error(400, "Missing Sec-WebSocket-Key")
            return
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()
        ).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        proto = self.headers.get("Sec-WebSocket-Protocol")
        if proto:
            self.send_header("Sec-WebSocket-Protocol", proto.split(",")[0].strip())
        self.end_headers()
        self.wfile.flush()
        browser = self.connection

        # 2) Open a WebSocket to ComfyUI ourselves (act as a client).
        host, port = comfy_host_port()
        try:
            upstream = socket.create_connection((host, port), timeout=ARGS.timeout)
        except OSError as e:
            try:
                browser.close()
            except OSError:
                pass
            self.log_message("ws upstream connect failed: %s", e)
            return
        cli_key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ) % (self.path, host, port, cli_key)
        try:
            upstream.sendall(req.encode())
            # read until end of upstream response headers
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = upstream.recv(4096)
                if not chunk:
                    break
                buf += chunk
            head, _, leftover = buf.partition(b"\r\n\r\n")
            if b"101" not in head.split(b"\r\n", 1)[0]:
                self.log_message("ws upstream did not upgrade: %r", head[:80])
                upstream.close()
                browser.close()
                return
            if leftover:
                browser.sendall(leftover)  # any frames ComfyUI already sent
        except OSError as e:
            self.log_message("ws upstream handshake failed: %s", e)
            try:
                upstream.close()
                browser.close()
            except OSError:
                pass
            return

        # 3) Raw bidirectional relay. We never parse frames: ws frames are just
        #    bytes, and each side already did a valid handshake, so forwarding
        #    bytes verbatim (browser frames stay masked, server frames unmasked)
        #    is correct.
        self.close_connection = True
        socks = [browser, upstream]
        try:
            while True:
                r, _, _ = select.select(socks, [], [], 60)
                if not r:
                    continue
                for s in r:
                    try:
                        data = s.recv(65536)
                    except OSError:
                        return
                    if not data:
                        return
                    dst = upstream if s is browser else browser
                    try:
                        dst.sendall(data)
                    except OSError:
                        return
        finally:
            for s in socks:
                try:
                    s.close()
                except OSError:
                    pass


# ---- raster -> hybrid SVG vectorization (lazy deps: pillow, vtracer;        --
#      optional masking via SAM if configured, else OpenCV GrabCut) -----------
def _is_flat(crop, threshold=11.0):
    # Cheap "vectorizable?" test: a flat/graphic region is well-approximated by
    # a tiny palette (low reconstruction error); a photo is not.
    from PIL import ImageChops
    small = crop.resize((96, 96))
    approx = small.quantize(colors=16).convert("RGB")
    diff = ImageChops.difference(small, approx).convert("L")
    mean = sum(diff.getdata()) / (96 * 96)
    return mean < threshold


def _rotate_image(img, deg):
    from PIL import Image
    if deg == 90:
        return img.transpose(Image.ROTATE_270)   # 90 clockwise
    if deg == 180:
        return img.transpose(Image.ROTATE_180)
    if deg == 270:
        return img.transpose(Image.ROTATE_90)     # 90 counter-clockwise
    return img


def _rotate_bbox(b, deg):
    # bbox in normalized 0..1000 per axis; rotate to match a clockwise image rotation
    x1, y1, x2, y2 = b
    if deg == 90:
        return [1000 - y2, x1, 1000 - y1, x2]
    if deg == 180:
        return [1000 - x2, 1000 - y2, 1000 - x1, 1000 - y1]
    if deg == 270:
        return [y1, 1000 - x2, y2, 1000 - x1]
    return [x1, y1, x2, y2]


def _trace_crop(crop):
    import vtracer
    cw, ch = crop.size
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    data = buf.getvalue()
    try:
        svg = vtracer.convert_raw_image_to_svg(data, img_format="png", colormode="color")
    except TypeError:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(data)
            inp = f.name
        outp = inp + ".svg"
        vtracer.convert_image_to_svg_py(inp, outp)
        with open(outp) as fh:
            svg = fh.read()
        for p in (inp, outp):
            try:
                os.unlink(p)
            except OSError:
                pass
    m = re.search(r"<svg[^>]*>(.*)</svg>", svg, re.S)
    return (m.group(1) if m else ""), cw, ch


# --- masking backends: SAM (preferred, if configured) else OpenCV GrabCut ----
_SAM_PREDICTOR = None
_SAM_TRIED = False

def _sam_predictor():
    """Lazily build a SAM predictor if SAM_CHECKPOINT is set and importable.

    Logs exactly why SAM does or doesn't load (to comfy_proxy.log via stderr).
    SAM is optional — on any failure we return None and the caller falls back to
    the foreground heuristic — but the failure is no longer silent, so a user
    setting it up can see what's wrong instead of guessing.
    """
    global _SAM_PREDICTOR, _SAM_TRIED
    if _SAM_TRIED:
        return _SAM_PREDICTOR
    _SAM_TRIED = True

    def log(msg):
        sys.stderr.write("[sam] %s\n" % msg)
        sys.stderr.flush()

    ckpt = os.environ.get("SAM_CHECKPOINT")
    mtype = os.environ.get("SAM_MODEL_TYPE", "vit_b")
    if not ckpt:
        log("SAM_CHECKPOINT not set — using the foreground heuristic for masks. "
            "Set SAM_CHECKPOINT (and SAM_MODEL_TYPE) to enable SAM.")
        return None
    if not os.path.isfile(ckpt):
        log("SAM_CHECKPOINT=%r does not exist — falling back to the heuristic. "
            "Check the path (it must be absolute for the service)." % ckpt)
        return None
    try:
        import torch
        from segment_anything import sam_model_registry, SamPredictor
        if mtype not in sam_model_registry:
            log("SAM_MODEL_TYPE=%r is not one of %s — falling back to the heuristic."
                % (mtype, list(sam_model_registry.keys())))
            return None
        sam = sam_model_registry[mtype](checkpoint=ckpt)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        sam.to(device)
        _SAM_PREDICTOR = SamPredictor(sam)
        log("loaded SAM (%s) on %s from %s" % (mtype, device, ckpt))
    except ImportError as e:
        log("SAM deps missing (%s) — install into the proxy's venv: "
            "pip install torch torchvision and the segment-anything package. "
            "Falling back to the heuristic." % e)
        _SAM_PREDICTOR = None
    except Exception as e:
        log("SAM failed to load (%s: %s) — common cause is SAM_MODEL_TYPE not "
            "matching the checkpoint (vit_b/vit_l/vit_h). Falling back to the "
            "heuristic." % (type(e).__name__, e))
        _SAM_PREDICTOR = None
    return _SAM_PREDICTOR


def _sam_mask(crop):
    pred = _sam_predictor()
    if pred is None:
        return None
    try:
        import numpy as np
        arr = np.array(crop.convert("RGB"))
        h, w = arr.shape[:2]
        pred.set_image(arr)
        box = np.array([1, 1, w - 1, h - 1])
        masks, scores, _ = pred.predict(box=box, multimask_output=True)
        return masks[int(np.argmax(scores))].astype("uint8")
    except Exception:
        return None


def _flat_foreground_mask(crop):
    # For flat regions (text/logos/flat art) the "ink" is what differs from the
    # local background. Estimate background from the border pixels, then keep
    # pixels far from it. Works far better than GrabCut for thin glyphs.
    try:
        import numpy as np
    except Exception:
        return None
    try:
        arr = np.array(crop.convert("RGB")).astype("int16")
        h, w = arr.shape[:2]
        if w < 8 or h < 8:
            return None
        border = np.concatenate([arr[0, :, :], arr[-1, :, :], arr[:, 0, :], arr[:, -1, :]], axis=0)
        bg = np.median(border, axis=0)
        dist = np.sqrt(((arr - bg) ** 2).sum(axis=2))
        m = (dist > 36.0).astype("uint8")
        frac = float(m.mean())
        if frac < 0.004 or frac > 0.97:
            return None  # nothing distinct, or element fills the box -> no clip
        try:
            import cv2
            k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
            m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, k, iterations=1)
            # Dilate a little extra so a clip built from this mask sits slightly
            # outside the ink and never shaves glyph edges (ascenders, thin strokes).
            m = cv2.dilate(m, k, iterations=2)
        except Exception:
            pass
        return m
    except Exception:
        return None


def _region_mask(crop, backend="auto"):
    # backend: "heuristic" -> always the flat-foreground heuristic;
    #          "auto"      -> SAM if configured, else the heuristic.
    if backend == "heuristic":
        return _flat_foreground_mask(crop)
    m = _sam_mask(crop)
    if m is None:
        m = _flat_foreground_mask(crop)
    return m


def _mask_to_polys(m):
    import cv2
    h, w = m.shape[:2]
    cnts, _ = cv2.findContours((m * 255).astype("uint8"), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    polys, area_min = [], max(4.0, w * h * 0.0008)
    for c in cnts:
        if cv2.contourArea(c) < area_min:
            continue
        approx = cv2.approxPolyDP(c, max(0.75, 0.004 * cv2.arcLength(c, True)), True)
        if approx.shape[0] < 3:
            continue
        polys.append(" ".join("%d,%d" % (int(p[0][0]), int(p[0][1])) for p in approx))
    return polys


def _strip_bg_paths(inner, cw, ch, thresh=0.92):
    """Drop VTracer paths whose bounding box spans ~the whole crop — its
    full-canvas background base layer and any full-width "frame" layers. The
    raster base already holds that background, so keeping these as opaque vector
    fills would just paint a box over the photo. What's left is the crisp
    foreground detail (glyphs, logo shapes), transparent everywhere else."""
    area = max(1.0, float(cw) * float(ch))
    out = []
    for tag in re.finditer(r'<path\b[^>]*?/>', inner):
        s = tag.group(0)
        dm = re.search(r'\bd="([^"]+)"', s)
        if dm:
            nums = re.findall(r'-?\d+(?:\.\d+)?', dm.group(1))
            xs = [float(n) for n in nums[0::2]]
            ys = [float(n) for n in nums[1::2]]
            if xs and ys and ((max(xs) - min(xs)) * (max(ys) - min(ys)) / area) >= thresh:
                continue
        out.append(s)
    return "".join(out)


def vectorize_image(img_bytes, elements, options):
    """Build a hybrid SVG: full render as a raster base, flat regions traced to
    vector and overlaid in place. Routing: text/logo always vector, subject/bg
    always raster, everything else by the flatness heuristic. When masking is
    enabled (and a backend is available), each vector overlay is clipped to the
    element's actual shape so it doesn't paint a rectangle over the photo."""
    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    rotate = int(options.get("rotate", 0) or 0) % 360
    if rotate in (90, 180, 270):
        img = _rotate_image(img, rotate)
    W, H = img.size
    flat_threshold = float(options.get("flat_threshold", 11.0))
    backend = (options.get("mask_backend") or "auto").lower()
    use_mask = backend != "none"
    flat_types = {"text", "logo"}
    never_types = {"subject", "bg"}
    vec_parts = []
    stats = {"width": W, "height": H, "vectorized": 0, "raster": 0, "masked": 0, "regions": []}
    for idx, el in enumerate(elements):
        bbox = el.get("bbox") or []
        if len(bbox) != 4:
            continue
        try:
            x1, y1, x2, y2 = [float(v) for v in bbox]
        except Exception:
            continue
        if rotate in (90, 180, 270):
            x1, y1, x2, y2 = _rotate_bbox([x1, y1, x2, y2], rotate)
        px1 = max(0, min(W, round(min(x1, x2) / 1000.0 * W)))
        px2 = max(0, min(W, round(max(x1, x2) / 1000.0 * W)))
        py1 = max(0, min(H, round(min(y1, y2) / 1000.0 * H)))
        py2 = max(0, min(H, round(max(y1, y2) / 1000.0 * H)))
        if px2 - px1 < 4 or py2 - py1 < 4:
            continue
        # Pad the trace crop a little (clamped to the image). Titles often have
        # glyphs whose tops/edges sit right on the element's bbox boundary; without
        # margin VTracer shaves them off and the crisp vector ends up shorter than
        # the raster underneath. The overlay is positioned at the padded box so the
        # glyphs still land in the right place.
        pad = max(4, round(min(px2 - px1, py2 - py1) * 0.06))
        ax1 = max(0, px1 - pad)
        ay1 = max(0, py1 - pad)
        ax2 = min(W, px2 + pad)
        ay2 = min(H, py2 + pad)
        t = (el.get("type") or "").lower()
        crop = img.crop((ax1, ay1, ax2, ay2))
        mode = (el.get("vectorize") or "auto").lower()
        if mode == "off":
            do_vec = False
        elif mode == "on":
            do_vec = True
        else:
            do_vec = (t in flat_types) or (t not in never_types and _is_flat(crop, flat_threshold))
        region = {"type": t, "x": px1, "y": py1, "w": px2 - px1, "h": py2 - py1, "vector": False}
        if do_vec:
            try:
                inner, cw, ch = _trace_crop(crop)
                # VTracer's bottom layer is a full-canvas background fill (and busy
                # regions add full-width "frame" layers). The raster base already
                # holds that background, so an opaque vector copy of it just paints a
                # box over the photo. Drop full-coverage paths so the overlay carries
                # only the crisp foreground detail and is transparent elsewhere.
                inner = _strip_bg_paths(inner, cw, ch)
                clip_defs, g_open, g_close = "", "", ""
                masked_ok = True
                is_flat_crop = _is_flat(crop, flat_threshold)
                # A flat crop is all graphic: stripping the background base already
                # isolates the glyphs/logo, so DON'T clip it — a polygon clip only
                # risks shaving letter edges (ascenders, thin strokes). Masking is
                # only needed on a non-flat crop, to cut away the photographic parts.
                if use_mask and not is_flat_crop:
                    m = _region_mask(crop, backend)
                    frac = float(m.mean()) if m is not None else 1.0
                    polys = _mask_to_polys(m) if m is not None else []
                    # Trust the mask only when it isolates a minority foreground (the
                    # ink/logo); a mask covering most of the box hasn't separated the
                    # graphic from the photo.
                    good = bool(polys) and frac < 0.6
                    if good:
                        cid = "vclip%d" % idx
                        clip_defs = '<defs><clipPath id="%s">%s</clipPath></defs>' % (
                            cid, "".join('<polygon points="%s"/>' % p for p in polys))
                        g_open, g_close = '<g clip-path="url(#%s)">' % cid, "</g>"
                        region["masked"] = True
                        stats["masked"] += 1
                    else:
                        masked_ok = False
                if use_mask and not masked_ok and mode != "on" and not is_flat_crop:
                    # Couldn't isolate the graphic and the crop isn't a flat graphic
                    # (it has photographic detail): keep this region as the raster base
                    # rather than tracing the photo into a box of garbage paths.
                    region["raster_fallback"] = True
                    stats["raster"] += 1
                else:
                    vec_parts.append(
                        '<svg x="%d" y="%d" width="%d" height="%d" viewBox="0 0 %d %d" preserveAspectRatio="none">%s%s%s%s</svg>'
                        % (ax1, ay1, ax2 - ax1, ay2 - ay1, cw, ch, clip_defs, g_open, inner, g_close))
                    region["vector"] = True
                    stats["vectorized"] += 1
            except Exception as e:
                region["error"] = str(e)
                stats["raster"] += 1
        else:
            stats["raster"] += 1
        stats["regions"].append(region)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode()
    # Declare the xlink namespace and reference the embedded raster with
    # xlink:href — Adobe Illustrator only resolves the namespaced form, while
    # browsers accept it too (so the in-app preview still works).
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="%d" height="%d" viewBox="0 0 %d %d">' % (W, H, W, H)]
    parts.append('<image x="0" y="0" width="%d" height="%d" xlink:href="data:image/jpeg;base64,%s"/>' % (W, H, b64))
    parts.extend(vec_parts)
    parts.append("</svg>")
    return "".join(parts), stats


def _load_env_file(path):
    """Load simple KEY=VALUE lines from an untracked env file into os.environ
    (without overriding variables already set in the environment). Keeps the
    real ComfyUI address out of the tracked code."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


def main():
    global ARGS
    here = os.path.dirname(os.path.abspath(__file__))
    _load_env_file(os.environ.get("COMFY_PROXY_ENV", os.path.join(here, "comfy_proxy.env")))
    p = argparse.ArgumentParser(description="Local same-origin proxy for the Ideogrammar editor -> ComfyUI")
    p.add_argument("--comfy", default=os.environ.get("COMFY_URL", "http://127.0.0.1:8188"), help="ComfyUI base URL (env: COMFY_URL)")
    p.add_argument("--host", default=os.environ.get("PROXY_HOST", "127.0.0.1"), help="address to bind (use 0.0.0.0 to expose on LAN; env: PROXY_HOST)")
    p.add_argument("--port", type=int, default=int(os.environ.get("PROXY_PORT", "8189")), help="port to serve on (env: PROXY_PORT)")
    p.add_argument("--html", default=os.path.join(here, "index.html"), help="path to index.html")
    p.add_argument("--output-dir", default="", help="ComfyUI output/ folder (readable by this proxy) — enables gallery recovery via the editor's Rescan button, surviving ComfyUI restarts")
    p.add_argument("--ref-dir", default=os.environ.get("IDEOGRAMMAR_REF_DIR", os.path.join(here, ".ideogrammar_refimg")), help="where to cache reference images for the before/after compare slider (env: IDEOGRAMMAR_REF_DIR)")
    p.add_argument("--timeout", type=float, default=600.0, help="upstream timeout (s)")
    p.add_argument("--verbose", action="store_true", help="log every request")
    ARGS = p.parse_args()

    srv = ThreadingHTTPServer((ARGS.host, ARGS.port), Handler)
    srv.daemon_threads = True
    url = "http://%s:%s/" % ("localhost" if ARGS.host in ("127.0.0.1", "0.0.0.0") else ARGS.host, ARGS.port)
    print("Ideogrammar proxy running")
    print("  open:       %s" % url)
    print("  forwarding: %s" % ARGS.comfy)
    if ARGS.output_dir:
        ok = os.path.isdir(ARGS.output_dir)
        print("  output dir: %s%s" % (ARGS.output_dir, "" if ok else "  (NOT a readable folder — gallery recovery disabled)"))
    print("  ref images: %s" % ARGS.ref_dir)
    print("  editor Server URL: leave blank (uses this origin) or set %s" % url.rstrip("/"))
    _sam_ckpt = os.environ.get("SAM_CHECKPOINT")
    if not _sam_ckpt:
        print("  vectorize masks: foreground heuristic (SAM not configured; set SAM_CHECKPOINT to enable)")
    elif not os.path.isfile(_sam_ckpt):
        print("  vectorize masks: heuristic — SAM_CHECKPOINT does not exist: %s" % _sam_ckpt)
    else:
        print("  vectorize masks: SAM configured (%s, %s) — loads on first vectorize"
              % (os.environ.get("SAM_MODEL_TYPE", "vit_b"), _sam_ckpt))
    print("Press Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
