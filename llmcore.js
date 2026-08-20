/* ======================================================================
 * llmcore.js — shared engine for Ideogrammar (index.html) and LLMCam
 * (llmcam.html). Plain (non-module) script: everything here is a global,
 * loaded before each page's own script. Owns the ComfyUI workflow template,
 * the resolution/prompt-building math, the LLM call, config, and the render
 * plumbing. Pages provide a "layout" object and the editor/camera UI.
 *
 * A "layout" is: { high_level_description, style_description,
 *   compositional_deconstruction: { background, elements: [
 *     { type, bbox:[x1,y1,x2,y2], desc, color_palette } ] } }
 * (bbox is X-first, 0..1000, as the LLM emits and the editor stores).
 * ====================================================================== */
"use strict";

/* ---- LLM provider config (localStorage: ideogram_llm_cfg) ---- */
const CFG_KEY = "ideogram_llm_cfg";
const PROVIDER_DEFAULTS = {
  openrouter: { baseUrl: "https://openrouter.ai/api/v1", model: "anthropic/claude-3.5-sonnet" },
  local:      { baseUrl: "http://localhost:8081/v1",     model: "local-model" }
};
function loadCfg() {
  let cfg;
  try { cfg = JSON.parse(localStorage.getItem(CFG_KEY)); } catch (_) {}
  if (!cfg || typeof cfg !== "object") cfg = { provider: "openrouter", baseUrl: PROVIDER_DEFAULTS.openrouter.baseUrl, apiKey: "", model: PROVIDER_DEFAULTS.openrouter.model };
  if (!ELEMENT_LEVELS[cfg.elements]) cfg.elements = "balanced";   // detail: how many elements
  if (!DESC_LEVELS[cfg.desc]) cfg.desc = "balanced";             // detail: how verbose each desc
  if (typeof cfg.temperature !== "number" || cfg.temperature < 0 || cfg.temperature > 2) cfg.temperature = 0.7;
  return cfg;
}
function saveCfg(cfg) { localStorage.setItem(CFG_KEY, JSON.stringify(cfg)); }

/* ---- generation "detail" controls (element count + description richness) ---- */
const ELEMENT_LEVELS = {
  few:      { label: "Few (2–3)",       rule: "Decompose the scene into only 2 to 3 elements — just the most important parts." },
  balanced: { label: "Balanced (3–6)",  rule: "Decompose the scene into 3 to 6 elements." },
  detailed: { label: "Detailed (6–10)", rule: "Decompose the scene into 6 to 10 elements, breaking it into more distinct parts." },
  maximal:  { label: "Maximal (10–16)", rule: "Decompose the scene into 10 to 16 elements, breaking it very finely into many distinct parts." }
};
const DESC_LEVELS = {
  brief:    { label: "Brief",    rule: "Keep each element's desc to a short phrase of a few words." },
  balanced: { label: "Balanced", rule: "Write each element's desc as one concrete, specific sentence." },
  rich:     { label: "Rich",     rule: "Write each element's desc as a vivid, richly detailed 1–2 sentences covering materials, textures, colors and spatial relations." }
};
/* ---- style presets (shared by LLMCam capture + the editor's Refine) ----
   Each guide() returns a plain-language instruction that preserves composition
   and changes only what's depicted — usable both as image-generation guidance
   and as a Refine change-request on an existing setup. */
const STYLE_PRESETS = {
  faithful: { label: null, options: null, guide: () => "Render this exact scene naturally and photo-realistically with no added artistic style or stylization; keep the same composition, framing and element positions." },
  time: {
    label: "Era", options: ["Prehistoric caveman", "Mesopotamia", "Babylon", "Ancient Egypt", "Ancient Rome", "Medieval", "Renaissance", "Victorian (1890s)", "Roaring 1920s", "1950s", "1960s", "1970s", "1980s", "1990s", "2000s", "2010s", "Cyberpunk near-future", "Far future / sci-fi"],
    guide: v => `Time travel: keep the exact same composition, framing and element positions, but depict the scene as a period-accurate ${v} version — adjust clothing, technology, vehicles, architecture, materials and color treatment to that era.`
  },
  style: {
    label: "Style", options: ["Oil painting", "Watercolor", "Anime", "Pixel art", "Comic book", "3D render", "Pencil sketch", "Pop art", "Low-poly", "Claymation"],
    guide: v => `Re-render the scene in this medium/style: ${v}. Keep the exact same composition and layout; only change the rendering style.`
  },
  genre: {
    label: "Genre / mood", options: ["Cyberpunk", "Film noir", "High fantasy", "Post-apocalyptic", "Vaporwave", "Steampunk", "Horror", "Solarpunk", "Western", "Fairy tale"],
    guide: v => `Re-theme the scene as ${v}. Keep the exact same composition and layout; restyle the content, lighting and mood to match the theme.`
  }
};
function styleGuidance(modeKey, subValue) {
  const p = STYLE_PRESETS[modeKey];
  if (!p || !p.guide) return "";
  return p.options ? p.guide(subValue) : p.guide();
}

// Instruction appended to the system prompt for fresh generation (text or image)
// so the model honors the user's chosen element count and description richness.
function detailDirective(cfg) {
  cfg = cfg || loadCfg();
  const e = ELEMENT_LEVELS[cfg.elements] || ELEMENT_LEVELS.balanced;
  const d = DESC_LEVELS[cfg.desc] || DESC_LEVELS.balanced;
  return `\n\nDETAIL SETTINGS (these override any element-count or description guidance above):\n- ${e.rule}\n- ${d.rule}`;
}

/* ---- system prompts ---- */
const SYSTEM_PROMPT = `You are a layout designer that turns a natural-language image description into a structured JSON "prompt" for the Ideogram 4 image model.

Output ONLY a single JSON object (no markdown, no commentary) with EXACTLY this shape:
{
  "high_level_description": "one vivid sentence describing the whole image",
  "style_description": {
    "aesthetics": "art direction, genre, texture and mood keywords",
    "lighting": "lighting description",
    "photo": "camera / lens / film / rendering notes",
    "medium": "the medium, e.g. 'digital illustration', 'oil painting', '35mm photography'",
    "color_palette": ["#RRGGBB", "..."]
  },
  "compositional_deconstruction": {
    "background": "description of the backdrop",
    "elements": [
      {
        "type": "obj" | "subject" | "text" | "logo" | "bg",
        "bbox": [x1, y1, x2, y2],
        "desc": "what this element is and where it sits",
        "color_palette": ["#RRGGBB", "..."],
        "font": "OPTIONAL — typography style for text/logo elements only (e.g. 'bold geometric sans-serif lettering'); omit for non-text elements"
      }
    ]
  }
}

Rules:
- The image canvas is a 1000 x 1000 coordinate space. Origin (0,0) is the TOP-LEFT. x grows right, y grows down. Every bbox is [x1, y1, x2, y2] with 0 <= x1 < x2 <= 1000 and 0 <= y1 < y2 <= 1000.
- Place elements sensibly: titles near the top, taglines/credits near the bottom, the main subject roughly centered. Boxes may overlap when layered (e.g. text over a subject).
- Use 3 to 6 elements unless the description clearly calls for more. Any text the image should literally show goes in a "text" element with the exact words in its desc (e.g. reading 'NEON TIDE').
- All colors are uppercase 6-digit hex like "#1A2B3C". Give each element a small palette (1-4 colors) that matches it.
- For "text" and "logo" elements you MAY add a "font" describing the typography (weight, serif/sans/script/display, era or mood) — e.g. "elegant high-contrast serif lettering". Omit "font" entirely for non-text elements.
- Be concrete and specific. Return valid JSON only.`;

const IMAGE_SYSTEM_PROMPT = SYSTEM_PROMPT + `

IMAGE MODE: You are given an actual IMAGE (and possibly some extra text guidance). Look at the image carefully and reconstruct it as the JSON schema above: capture its overall subject in high_level_description, its art style / medium / lighting / dominant color palette in style_description, and decompose it into the key positioned elements — the background, the main subject(s), distinct objects, logos, and any literal text you can READ in the image (put the exact words in the text element's desc). Estimate each element's bbox from where it actually sits in the image, mapped onto the 1000x1000 canvas. PRESERVE THE COMPOSITION AND LAYOUT exactly (positions and sizes of everything). If extra guidance asks to change the era/style/genre/content, apply that transformation to WHAT each element depicts while keeping WHERE it sits identical.`;

/* ---- LLM call ---- */
// POST to /chat/completions and return the message content. Some OpenAI-compatible
// servers reject response_format:{type:"json_object"} (LM Studio, ninfer) or a
// too-large max_tokens (vLLM) with a 400; in that case retry once without the
// offending field (extractJSON still parses a plain-text reply).
// Local backends often can't answer the browser's CORS preflight, so when the
// page is served by the proxy, route local-provider calls through its
// /ideogrammar/llm/ relay (same origin => no CORS) and name the real backend
// in a header. OpenRouter supports CORS fine, so it stays direct. A file://
// page has no proxy to lean on and keeps the old direct behavior.
function llmEndpoint(cfg, path) {
  const proxied = cfg.provider === "local" && (location.protocol === "http:" || location.protocol === "https:");
  return proxied
    ? { url: location.origin + "/ideogrammar/llm" + path, extraHeaders: { "X-Llm-Base-Url": cfg.baseUrl } }
    : { url: cfg.baseUrl + path, extraHeaders: {} };
}
async function chatCompletion(cfg, headers, body) {
  const ep = llmEndpoint(cfg, "/chat/completions");
  // Reasoning models (e.g. Qwen3) spend tokens "thinking" before the answer, and
  // some servers default to a small completion cap (ninfer: 1024) — the reply then
  // comes back truncated or with an empty content. Always ask for enough room.
  if (body.max_tokens == null) body = { ...body, max_tokens: 8192 };
  const send = b => fetch(ep.url, { method: "POST", headers: Object.assign({}, headers, ep.extraHeaders), body: JSON.stringify(b) });
  const errDetail = async res => {
    try { const j = await res.json(); return j.error?.message || JSON.stringify(j.error || j); }
    catch (_) { return await res.text().catch(() => ""); }
  };
  let res = await send(body);
  if (!res.ok) {
    const detail = await errDetail(res);
    const badFormat = body.response_format && /response_format/i.test(detail);
    const badMaxTok = body.max_tokens != null && /max_tokens|max_completion_tokens/i.test(detail);
    if (res.status === 400 && (badFormat || badMaxTok)) {
      const retry = { ...body };
      if (badFormat) delete retry.response_format;
      if (badMaxTok) delete retry.max_tokens;
      res = await send(retry);
      if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText} — ${await errDetail(res)}`);
    } else {
      throw new Error(`HTTP ${res.status} ${res.statusText}${detail ? " — " + detail : ""}`);
    }
  }
  const data = await res.json();
  const choice = data.choices?.[0];
  const content = choice?.message?.content;
  if (choice?.finish_reason === "length") throw new Error("The model hit the completion token limit" + (content ? " and the reply is cut off" : " while still reasoning") + ". Raise the server's max output tokens, or use a model that thinks less.");
  if (!content) throw new Error("Empty response from model.");
  return content;
}
function extractJSON(text) {
  if (typeof text !== "string") return null;
  let t = text.trim();
  const fence = t.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fence) t = fence[1].trim();
  try { return JSON.parse(t); } catch (_) {}
  const start = t.indexOf("{"), end = t.lastIndexOf("}");
  if (start !== -1 && end > start) { try { return JSON.parse(t.slice(start, end + 1)); } catch (_) {} }
  return null;
}

/* ---- ComfyUI workflow template ---- */
const WORKFLOW_TEMPLATE = {
  "25": { inputs: { images: ["98:13", 0] }, class_type: "PreviewImage", _meta: { title: "Preview Image" } },
  "37": { inputs: { aspect_ratio: "1:1 (Square)", megapixels: 2, multiple: 8 }, class_type: "ResolutionSelector", _meta: { title: "Resolution Selector" } },
  "98:9": { inputs: { vae_name: "flux2-vae.safetensors" }, class_type: "VAELoader", _meta: { title: "Load VAE" } },
  "98:10": { inputs: { conditioning: ["98:24", 0] }, class_type: "ConditioningZeroOut", _meta: { title: "ConditioningZeroOut" } },
  "98:11": { inputs: { width: ["98:31", 1], height: ["98:32", 1], batch_size: 1 }, class_type: "EmptyFlux2LatentImage", _meta: { title: "Empty Flux 2 Latent" } },
  "98:12": { inputs: { noise: ["98:18", 0], guider: ["98:155", 0], sampler: ["98:16", 0], sigmas: ["98:17", 0], latent_image: ["98:11", 0] }, class_type: "SamplerCustomAdvanced", _meta: { title: "SamplerCustomAdvanced" } },
  "98:13": { inputs: { samples: ["98:12", 0], vae: ["98:9", 0] }, class_type: "VAEDecode", _meta: { title: "VAE Decode" } },
  "98:16": { inputs: { sampler_name: "res_multistep" }, class_type: "KSamplerSelect", _meta: { title: "KSamplerSelect" } },
  "98:17": { inputs: { steps: ["98:151", 1], width: ["98:31", 1], height: ["98:32", 1], mu: ["98:144", 0], std: ["98:146", 0] }, class_type: "Ideogram4Scheduler", _meta: { title: "Ideogram 4 Scheduler" } },
  "98:18": { inputs: { noise_seed: 453251084258580 }, class_type: "RandomNoise", _meta: { title: "RandomNoise" } },
  "98:23": { inputs: { unet_name: "ideogram4_fp8_scaled.safetensors", weight_dtype: "default" }, class_type: "UNETLoader", _meta: { title: "Load Diffusion Model" } },
  "98:24": { inputs: { text: "", clip: ["98:14", 0] }, class_type: "CLIPTextEncode", _meta: { title: "CLIP Text Encode (Positive Prompt)" } },
  "98:14": { inputs: { clip_name: "qwen3vl_8b_fp8_scaled.safetensors", type: "ideogram4", device: "default" }, class_type: "CLIPLoader", _meta: { title: "Load CLIP" } },
  "98:27": { inputs: { value: ["37", 0] }, class_type: "PrimitiveInt", _meta: { title: "Int (Width)" } },
  "98:28": { inputs: { value: ["37", 1] }, class_type: "PrimitiveInt", _meta: { title: "Int (Height)" } },
  "98:31": { inputs: { expression: "max(((a + 15) // 16) * 16, 256)", "values.a": ["98:27", 0] }, class_type: "ComfyMathExpression", _meta: { title: "Math Expression" } },
  "98:32": { inputs: { expression: "max(((a + 15) // 16) * 16, 256)", "values.a": ["98:28", 0] }, class_type: "ComfyMathExpression", _meta: { title: "Math Expression" } },
  "98:144": { inputs: { value: ["98:145", 0] }, class_type: "ComfyNumberConvert", _meta: { title: "Number Convert" } },
  "98:145": { inputs: { json_string: ["98:148", 0], key: "mu" }, class_type: "JsonExtractString", _meta: { title: "Extract Text from JSON" } },
  "98:146": { inputs: { value: ["98:150", 0] }, class_type: "ComfyNumberConvert", _meta: { title: "Number Convert" } },
  "98:147": { inputs: { json_string: '{"Quality":{"num_steps":48,"mu":0.0,"std":1.5,"preset_id":"V4_QUALITY_48"},"Default":{"num_steps":20,"mu":0.0,"std":1.75,"preset_id":"V4_DEFAULT_20"},"Turbo":{"num_steps":12,"mu":0.5,"std":1.75,"preset_id":"V4_TURBO_12"}}', key: ["98:156", 0] }, class_type: "JsonExtractString", _meta: { title: "Extract Text from JSON" } },
  "98:148": { inputs: { string: ["98:147", 0], find: "'", replace: '"' }, class_type: "StringReplace", _meta: { title: "Replace Text" } },
  "98:149": { inputs: { json_string: ["98:148", 0], key: "num_steps" }, class_type: "JsonExtractString", _meta: { title: "Extract Text from JSON" } },
  "98:150": { inputs: { json_string: ["98:148", 0], key: "std" }, class_type: "JsonExtractString", _meta: { title: "Extract Text from JSON" } },
  "98:151": { inputs: { value: ["98:149", 0] }, class_type: "ComfyNumberConvert", _meta: { title: "Number Convert" } },
  "98:154": { inputs: { unet_name: "ideogram4_unconditional_fp8_scaled.safetensors", weight_dtype: "default" }, class_type: "UNETLoader", _meta: { title: "Load Diffusion Model" } },
  "98:155": { inputs: { cfg: 7, model: ["98:157", 0], positive: ["98:24", 0], model_negative: ["98:154", 0], negative: ["98:10", 0] }, class_type: "DualModelGuider", _meta: { title: "Dual Model CFG Guider" } },
  "98:156": { inputs: { choice: "Turbo", index: 2, option1: "Quality", option2: "Default", option3: "Turbo", option4: "" }, class_type: "CustomCombo", _meta: { title: "Custom Combo" } },
  "98:157": { inputs: { cfg: 3, start_percent: 0.9, end_percent: 1, model: ["98:23", 0] }, class_type: "CFGOverride", _meta: { title: "CFG Override" } }
};
const NODE = { resolution: "37", seed: "98:18", preset: "98:156", guider: "98:155", cfgOverride: "98:157", sampler: "98:16", prompt: "98:24", clip: "98:14", diffModel: "98:23", uncondModel: "98:154", vae: "98:9", latent: "98:11", lora: "98:300", decode: "98:13", rtxsr: "98:400" };
const PRESET_INDEX = { Quality: 0, Default: 1, Turbo: 2 };
// Alternative scheduler setup (community "simple scheduler" tweak): insert
// ModelSamplingAuraFlow (shift) after the diffusion model, replace the
// Ideogram4Scheduler with a BasicScheduler ("simple"), use euler.
function makeWorkflowV2(v1) {
  const wf = JSON.parse(JSON.stringify(v1));
  wf["98:200"] = { inputs: { shift: 5, model: ["98:23", 0] }, class_type: "ModelSamplingAuraFlow", _meta: { title: "ModelSamplingAuraFlow" } };
  wf["98:157"].inputs.model = ["98:200", 0];
  wf["98:17"] = { inputs: { model: ["98:200", 0], scheduler: "simple", steps: ["98:151", 1], denoise: 1 }, class_type: "BasicScheduler", _meta: { title: "BasicScheduler" } };
  wf["98:16"].inputs.sampler_name = "euler";
  return wf;
}
const WORKFLOW_TEMPLATE_V2 = makeWorkflowV2(WORKFLOW_TEMPLATE);

/* ---- ComfyUI config (localStorage: ideogram_comfy_cfg) ---- */
const COMFY_KEY = "ideogram_comfy_cfg";
function defaultComfyCfg() {
  return {
    mode: "manual",
    url: "http://127.0.0.1:8188",
    _defaultsV: 4,
    direct: false, save: false, workflow: "v2",
    aspect_ratio: "1:1 (Square)", megapixels: 2,
    preset: "Turbo", seed: 453251084258580, randomize: true,
    cfg: 3, sampler_name: "euler",
    cfg_override: 3, start_percent: 0.9, end_percent: 1,
    diff_model: "ideogram4_fp8_scaled.safetensors",
    uncond_model: "ideogram4_unconditional_fp8_scaled.safetensors",
    lora_enabled: false, lora_name: "", lora_strength: 1,
    rtxsr_enabled: false, rtxsr_quality: "ULTRA", rtxsr_scale: 2,
    vae_name: "flux2-vae.safetensors",
    clip_name: "qwen3vl_8b_fp8_scaled.safetensors", clip_type: "ideogram4",
    batch_size: 1,
    queue_count: 1, queue_reseed: false, queue_regen: false, queue_guidance: ""
  };
}
let comfyCfg = loadComfyCfg();
function loadComfyCfg() {
  let c; try { c = JSON.parse(localStorage.getItem(COMFY_KEY)); } catch (_) {}
  const cfg = Object.assign(defaultComfyCfg(), c && typeof c === "object" ? c : {});
  const v = cfg._defaultsV || 0;
  if (v < 2) { cfg.workflow = "v2"; cfg.cfg = 3; cfg.sampler_name = "euler"; }
  if (v < 3) { cfg.queue_reseed = false; }   // queue seed randomization now opt-in
  if (v < 4) { cfg.queue_regen = false; }    // queue prompt regeneration now opt-in
  cfg._defaultsV = 4;
  return cfg;
}
function saveComfyCfg() { localStorage.setItem(COMFY_KEY, JSON.stringify(comfyCfg)); }

function comfyBase() {
  // Default: talk to whatever origin served this page (the proxy) -> no CORS.
  if (!comfyCfg.direct) {
    return (location.protocol === "http:" || location.protocol === "https:") ? location.origin : "";
  }
  return (comfyCfg.url || "").trim().replace(/\/+$/, "");
}

// Reference-image cache on the proxy (for the before/after compare slider): upload
// a data URL and get back a short content-addressed key, so the browser persists
// only the key instead of a base64 blob. Returns null if the proxy can't store it
// (e.g. direct-to-ComfyUI mode) — callers then fall back to the in-memory data URL.
const _refImgKeyCache = new Map();   // dataURL -> key (avoid re-uploading the same image)
async function uploadRefImage(dataUrl) {
  if (!dataUrl || !/^data:/.test(dataUrl)) return null;
  if (_refImgKeyCache.has(dataUrl)) return _refImgKeyCache.get(dataUrl);
  const base = comfyBase();
  if (!base) return null;
  try {
    const blob = await (await fetch(dataUrl)).blob();
    const r = await fetch(base + "/ideogrammar/refimg", { method: "POST", headers: { "Content-Type": blob.type || "image/jpeg" }, body: blob });
    if (!r.ok) return null;
    const key = (await r.json()).key || null;
    if (key) _refImgKeyCache.set(dataUrl, key);
    return key;
  } catch (_) { return null; }
}
function refImgUrl(key) { return key ? (comfyBase() + "/ideogrammar/refimg/" + encodeURIComponent(key)) : null; }

// Keep a value if it's a finite number within [lo,hi]; otherwise fall back.
function numInRange(v, lo, hi, dflt) {
  const n = parseFloat(v);
  return (isFinite(n) && n >= lo && n <= hi) ? n : dflt;
}
function sanitizeComfyCfg() {
  comfyCfg.megapixels   = numInRange(comfyCfg.megapixels, 0.1, 16, 2);
  comfyCfg.cfg          = numInRange(comfyCfg.cfg, 0, 100, 7);
  comfyCfg.cfg_override = numInRange(comfyCfg.cfg_override, 0, 100, 3);
  comfyCfg.start_percent = numInRange(comfyCfg.start_percent, 0, 1, 0.9);
  comfyCfg.end_percent   = numInRange(comfyCfg.end_percent, 0, 1, 1);
  comfyCfg.batch_size   = Math.round(numInRange(comfyCfg.batch_size, 1, 64, 1));
  comfyCfg.lora_strength = numInRange(comfyCfg.lora_strength, -10, 10, 1);
  comfyCfg.rtxsr_scale   = numInRange(comfyCfg.rtxsr_scale, 1, 4, 2);
  comfyCfg.queue_count  = Math.round(numInRange(comfyCfg.queue_count, 1, 100, 1));
}

function getClientId() {
  let id = localStorage.getItem("ideogram_comfy_client");
  if (!id) { id = "ideogrammar-" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10); localStorage.setItem("ideogram_comfy_client", id); }
  return id;
}
function clampSeed(v) { let n = Math.floor(Number(v)); if (!isFinite(n) || n < 0) n = 0; if (n > Number.MAX_SAFE_INTEGER) n = Number.MAX_SAFE_INTEGER; return n; }
function randomSeed() { return Math.floor(Math.random() * 1e15); }

/* ---- resolution + prompt building ---- */
function parseAspectRatio(s) {
  const m = String(s || "").match(/(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)/);
  if (m) { const a = parseFloat(m[1]), b = parseFloat(m[2]); if (a > 0 && b > 0) return a / b; }
  return 1;
}
const ASPECT_OPTIONS = ["1:1 (Square)", "3:2 (Photo)", "4:3 (Standard)", "16:9 (Widescreen)", "21:9 (Ultrawide)", "2:3 (Portrait Photo)", "3:4 (Portrait Standard)", "9:16 (Portrait Widescreen)"];
function nearestAspect(ar) {
  let best = ASPECT_OPTIONS[0], bestD = Infinity;
  for (const opt of ASPECT_OPTIONS) { const d = Math.abs(Math.log(parseAspectRatio(opt) / ar)); if (d < bestD) { bestD = d; best = opt; } }
  return best;
}
function aspectInfo(label) {
  label = label || comfyCfg.aspect_ratio || "1:1 (Square)";
  const ar = parseAspectRatio(label);
  const m = String(label).match(/\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?/);
  const ratio = m ? m[0].replace(/\s+/g, "") : "1:1";
  const orientation = ar > 1.02 ? "landscape" : ar < 0.98 ? "portrait" : "square";
  return { label, ar, ratio, orientation };
}
// Replicate the workflow's resolution math so the prompt can state the exact
// output dimensions. ResolutionSelector: area = megapixels * 1024^2, sides
// ~ sqrt(area * ar) rounded to a multiple of 8; EmptyFlux2Latent rounds up to
// 16 with a 256 floor. Verified: 3:2 @ 2MP -> 1776x1184.
function computeDims() {
  const ar = aspectInfo().ar;
  const area = (comfyCfg.megapixels || 2) * 1024 * 1024;
  const rs8 = x => Math.round(x / 8) * 8;
  const lat = x => Math.max(256, Math.floor((x + 15) / 16) * 16);
  return { width: lat(rs8(Math.sqrt(area * ar))), height: lat(rs8(Math.sqrt(area / ar))) };
}
// Turn an X-first 0..1000 layout into the diffusion-facing prompt object:
// Ideogram 4 was trained on a 0..1000 normalized grid in Gemini-style
// [ymin,xmin,ymax,xmax] order, so the only transform is transposing X/Y (the
// range stays 0..1000). canvas comes first so the model reads the frame up front.
function buildPromptObj(layout) {
  const info = aspectInfo(), d = computeDims();
  const sd = layout.style_description || {};
  const cd = layout.compositional_deconstruction || {};
  const elements = (Array.isArray(cd.elements) ? cd.elements : []).map(e => {
    const b = (Array.isArray(e.bbox) && e.bbox.length === 4) ? e.bbox.map(n => Math.round(+n || 0)) : [100, 100, 500, 500];
    const [x1, y1, x2, y2] = b;
    const out = { type: e.type || "obj", bbox: [y1, x1, y2, x2], desc: e.desc || "", color_palette: Array.isArray(e.color_palette) ? e.color_palette : [] };
    if (e.font) out.font = e.font;
    return out;
  });
  return {
    canvas: { width: d.width, height: d.height, aspect_ratio: info.ratio, orientation: info.orientation },
    high_level_description: layout.high_level_description || "",
    style_description: { aesthetics: sd.aesthetics || "", lighting: sd.lighting || "", photo: sd.photo || "", medium: sd.medium || "", color_palette: Array.isArray(sd.color_palette) ? sd.color_palette : [] },
    compositional_deconstruction: { background: cd.background || "", elements }
  };
}
// The diffusion model's only prompt is this text (CLIPTextEncode, NOT parsed as
// JSON). Lead with a strong, plain orientation + exact-dimension statement so
// the model doesn't guess orientation per-seed and rotate the result.
function buildPromptText(layout) {
  const obj = buildPromptObj(layout), info = aspectInfo(), d = obj.canvas;
  const coord = ` Element bboxes are [ymin,xmin,ymax,xmax] on a 0-1000 normalized grid (each axis 0-1000 independent of the ${d.width}x${d.height} pixel size), origin top-left, x right, y down; place each element exactly there, do not rotate or mirror the layout.`;
  let lead;
  if (info.orientation === "landscape") lead = `LANDSCAPE orientation: a wide horizontal ${info.ratio} image, ${d.width}x${d.height} pixels (wider than tall).` + coord;
  else if (info.orientation === "portrait") lead = `PORTRAIT orientation: a tall vertical ${info.ratio} image, ${d.width}x${d.height} pixels (taller than wide).` + coord;
  else lead = `SQUARE ${info.ratio} image, ${d.width}x${d.height} pixels.` + coord;
  return lead + "\n\n" + JSON.stringify(obj, null, 2);
}
function buildWorkflow(layout) {
  const base = comfyCfg.workflow === "v2" ? WORKFLOW_TEMPLATE_V2 : WORKFLOW_TEMPLATE;
  const wf = (typeof structuredClone === "function") ? structuredClone(base) : JSON.parse(JSON.stringify(base));
  wf[NODE.prompt].inputs.text = buildPromptText(layout);
  wf[NODE.resolution].inputs.aspect_ratio = comfyCfg.aspect_ratio;
  wf[NODE.resolution].inputs.megapixels = comfyCfg.megapixels;
  wf[NODE.seed].inputs.noise_seed = comfyCfg.seed;
  wf[NODE.preset].inputs.choice = comfyCfg.preset;
  wf[NODE.preset].inputs.index = PRESET_INDEX[comfyCfg.preset] ?? 2;
  wf[NODE.guider].inputs.cfg = comfyCfg.cfg;
  wf[NODE.sampler].inputs.sampler_name = comfyCfg.sampler_name;
  wf[NODE.cfgOverride].inputs.cfg = comfyCfg.cfg_override;
  wf[NODE.cfgOverride].inputs.start_percent = comfyCfg.start_percent;
  wf[NODE.cfgOverride].inputs.end_percent = comfyCfg.end_percent;
  wf[NODE.diffModel].inputs.unet_name = comfyCfg.diff_model;
  wf[NODE.uncondModel].inputs.unet_name = comfyCfg.uncond_model;
  wf[NODE.vae].inputs.vae_name = comfyCfg.vae_name;
  wf[NODE.clip].inputs.clip_name = comfyCfg.clip_name;
  wf[NODE.clip].inputs.type = comfyCfg.clip_type;
  wf[NODE.latent].inputs.batch_size = comfyCfg.batch_size;
  // Optional model-only LoRA: splice it between the diffusion model and whatever
  // consumes it (CFGOverride in v1, ModelSamplingAuraFlow in v2). Only the
  // conditioned model is patched — the unconditional negative model is left bare.
  if (comfyCfg.lora_enabled && comfyCfg.lora_name) {
    for (const node of Object.values(wf)) {
      const m = node.inputs && node.inputs.model;
      if (Array.isArray(m) && m[0] === NODE.diffModel) node.inputs.model = [NODE.lora, 0];
    }
    wf[NODE.lora] = { inputs: { lora_name: comfyCfg.lora_name, strength_model: comfyCfg.lora_strength, model: [NODE.diffModel, 0] }, class_type: "LoraLoaderModelOnly", _meta: { title: "Load LoRA (Model Only)" } };
  }
  if (comfyCfg.save) wf["25"] = { inputs: { images: ["98:13", 0], filename_prefix: "ideogrammar" }, class_type: "SaveImage", _meta: { title: "Save Image" } };
  // Optional NVIDIA RTX Video Super Resolution: splice an upscaler between the
  // VAE decode and whatever consumes its image (Preview/Save). Same shape as the
  // LoRA splice — repoint existing consumers of the decode output to the new
  // node, then feed the decode into it. resize_type is the node's dynamic combo;
  // we use "scale by multiplier" with the chosen factor and quality.
  if (comfyCfg.rtxsr_enabled) {
    for (const node of Object.values(wf)) {
      const im = node.inputs && node.inputs.images;
      if (Array.isArray(im) && im[0] === NODE.decode) node.inputs.images = [NODE.rtxsr, 0];
    }
    // resize_type is a V3 dynamic combo: the chosen option's sub-input is
    // namespaced as "resize_type.scale" (not a bare "scale"), per ComfyUI's
    // prompt validator.
    wf[NODE.rtxsr] = { inputs: { images: [NODE.decode, 0], resize_type: "scale by multiplier", "resize_type.scale": comfyCfg.rtxsr_scale, quality: comfyCfg.rtxsr_quality }, class_type: "RTXVideoSuperResolution", _meta: { title: "RTX Video Super Resolution" } };
  }
  return wf;
}

/* ---- /object_info reconciliation (handle ComfyUI node updates) ---- */
let objectInfoCache = { base: null, info: null };
async function getObjectInfo(base) {
  if (objectInfoCache.base === base && objectInfoCache.info) return objectInfoCache.info;
  try { const r = await fetch(base + "/object_info", { cache: "no-store" }); if (r.ok) objectInfoCache = { base, info: await r.json() }; } catch (_) {}
  return objectInfoCache.base === base ? objectInfoCache.info : null;
}
// If a ComfyUI update adds a new REQUIRED input to a node we use (e.g.
// ResolutionSelector's "multiple"), fill it from the node's declared default.
function fillMissingRequiredInputs(wf, info) {
  if (!info) return;
  for (const node of Object.values(wf)) {
    const req = info[node.class_type]?.input?.required;
    if (!req) continue;
    for (const key of Object.keys(req)) {
      if (key in node.inputs) continue;
      const spec = req[key], opts = Array.isArray(spec) ? spec[1] : null;
      if (opts && Object.prototype.hasOwnProperty.call(opts, "default")) node.inputs[key] = opts.default;
      else if (Array.isArray(spec) && Array.isArray(spec[0]) && spec[0].length) node.inputs[key] = spec[0][0]; // COMBO: first option
    }
  }
}

// Submit a layout to ComfyUI and return the prompt_id (after reconciling node defs).
async function submitWorkflow(base, layout) {
  const wf = buildWorkflow(layout);
  fillMissingRequiredInputs(wf, await getObjectInfo(base));
  const res = await fetch(base + "/prompt", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompt: wf, client_id: getClientId() }) });
  if (!res.ok) {
    let detail = "";
    try {
      const j = await res.json();
      detail = j.error?.message || j.error?.type || "";
      if (j.node_errors && Object.keys(j.node_errors).length) {
        const ne = Object.values(j.node_errors).map(n => (n.errors || []).map(e => e.message).join(", ")).filter(Boolean).join("; ");
        if (ne) detail += (detail ? " — " : "") + ne;
      }
    } catch (_) { detail = await res.text().catch(() => ""); }
    throw new Error("HTTP " + res.status + (detail ? " — " + detail : ""));
  }
  const id = (await res.json()).prompt_id;
  if (!id) throw new Error("No prompt_id returned by ComfyUI.");
  return id;
}
