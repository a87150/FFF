// ==================== 全局状态 ====================
const $ = (id) => document.getElementById(id);   // getElementById 简写
let currentDir = window.MEDIA_DIR || "/";
let videoDuration = 0;
let availableEncoders = {};
let vaapiDeviceOk = false;
let currentTaskId = null;
let outputManuallyEdited = false;
let lastRecommendation = null;
let currentSrcInfo = null;
let browseMode = "browse";   // "browse"（目录浏览）| "scan"（探查结果）
let viewSeq = 0;             // 文件列表导航序号（丢弃过期响应，避免「..」点了不生效的竞态）

let batchQueue = [];
let isBatchRunning = false;
let batchCancelled = false;
let batchTotal = 0;
let batchDone = 0;

const FAV_KEY = "ffmpeg_web_favorites";
const OUT_DIR_KEY = "ffmpeg_web_output_dirs";
const SETTINGS_KEY = "ffmpeg_web_settings_v1";
const ACTIVE_TASK_KEY = "ffmpeg_web_active_task_v1";   // 单文件任务（刷新后恢复用）
const BATCH_STATE_KEY = "ffmpeg_web_batch_state_v1";   // 批量队列与运行状态（刷新后恢复用）

const SETTING_IDS = ["platform", "format", "qualityMode", "qualityValue", "bitrate",
                     "preset", "scale", "acodec", "abitrate", "extra",
                     "headTrim", "tailTrim"];

// 任务状态映射：icon 用于队列行，tag 用于进度区状态标，text 用于队列状态文案
const STATUS_MAP = {
  pending:   { icon: "⏳", tag: "排队中", text: "待处理" },
  running:   { icon: "▶",  tag: "转码中", text: "处理中" },
  completed: { icon: "✓",  tag: "完成",   text: "完成" },
  failed:    { icon: "✗",  tag: "失败",   text: "失败" },
  cancelled: { icon: "⏸",  tag: "已中止", text: "已中止" },
};

// ==================== Toast 提示 ====================
function toast(msg, type) {
  const box = $("toastBox");
  if (!box) return;
  const el = document.createElement("div");
  el.className = "toast" + (type ? " toast-" + type : "");
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => {
    el.classList.add("toast-out");
    setTimeout(() => el.remove(), 300);
  }, 3600);
}

// ==================== 进度显示（含速度 / 剩余时间） ====================
function formatEta(sec) {
  sec = Math.round(sec);
  if (!isFinite(sec) || sec <= 0) return "";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h > 0) return `${h} 小时 ${m} 分`;
  if (m > 0) return `${m} 分 ${s} 秒`;
  return `${s} 秒`;
}

function updateProgressUI(t) {
  $("progressFill").style.width = (t.progress || 0) + "%";
  $("progressPct").textContent = (t.progress || 0).toFixed(1) + "%";

  const infoEl = $("progressInfo");
  if (infoEl) {
    const parts = [];
    if (t.status === "running") {
      if (t.speed) parts.push(t.speed);
      if (t.eta > 0) parts.push("剩余 " + formatEta(t.eta));
    }
    infoEl.textContent = parts.join(" · ");
  }

  const stateEl = $("taskState");
  stateEl.textContent = (STATUS_MAP[t.status] || {}).tag || t.status;
  stateEl.className = "tag-status s-" + t.status;

  if (t.log) {
    const box = $("logBox");
    box.textContent = t.log;
    box.scrollTop = box.scrollHeight;
  }
}

// ==================== 路径工具 ====================
function detectSep(path) {
  if (path && path.includes("\\")) return "\\";
  return "/";
}

function splitPath(filepath) {
  if (!filepath) return { dir: "", filename: "", base: "", ext: ".mp4", sep: "/" };
  const lastSlash = Math.max(filepath.lastIndexOf("/"), filepath.lastIndexOf("\\"));
  const dir = lastSlash >= 0 ? filepath.substring(0, lastSlash) : "";
  const filename = lastSlash >= 0 ? filepath.substring(lastSlash + 1) : filepath;
  const dotIdx = filename.lastIndexOf(".");
  const base = dotIdx > 0 ? filename.substring(0, dotIdx) : filename;
  const ext = dotIdx > 0 ? filename.substring(dotIdx) : "";
  return { dir, filename, base, ext: ext || ".mp4", sep: detectSep(filepath) };
}

function joinPath(dir, filename) {
  if (!dir) return filename;
  const trimmed = dir.replace(/[\/\\]+$/, "");
  const sep = detectSep(dir);
  return trimmed + sep + filename;
}

// ==================== FFmpeg 检测 ====================
fetch("/api/ffmpeg_check").then(r => r.json()).then(d => {
  const el = $("ffmpegStatus");
  if (d.ok) {
    let ver = "";
    if (d.version) {
      const m = d.version.match(/ffmpeg version\s+(\S+)/i);
      ver = m ? m[1] : "";
    }
    const verHtml = ver ? ` <span class="ver">v${ver}</span>` : "";
    el.innerHTML = `✅ FFmpeg 已就绪${verHtml} &nbsp;<span style="color:#666">${d.path}</span>`;
  } else {
    el.innerHTML = `❌ 未找到 FFmpeg（${d.path}）`;
    el.style.color = "#ff6b6b";
  }
  availableEncoders = d.encoders || {};
  vaapiDeviceOk = !!d.vaapi_device;
  updatePlatformOptions();
  ensurePlatformValid();
  onPlatformChange(true);  // 保留已保存/已选择的 preset
});

// 已保存的平台如果在本机不可用，自动回退（优先软件编码，其次智能/复制）
function ensurePlatformValid() {
  const sel = $("platform");
  const current = sel.options[sel.selectedIndex];
  if (current && !current.disabled) return;
  for (const val of ["software", "smart", "copy"]) {
    const opt = Array.from(sel.options).find(o => o.value === val && !o.disabled);
    if (opt) { sel.value = val; return; }
  }
  const fallback = Array.from(sel.options).find(o => !o.disabled);
  if (fallback) sel.value = fallback.value;
}

function updatePlatformOptions() {
  const platformEncoders = {
    software: ["libx264", "libx265"],
    nvenc: ["h264_nvenc", "hevc_nvenc"],
    qsv: ["h264_qsv", "hevc_qsv"],
    amf: ["h264_amf", "hevc_amf"],
    vaapi: ["h264_vaapi", "hevc_vaapi"],
    copy: ["libx264"],
    smart: ["libx264"],
  };
  const sel = $("platform");
  for (const opt of sel.options) {
    const p = opt.value;
    if (p === "copy" || p === "smart") continue;
    const required = platformEncoders[p] || [];
    let ok = required.some(e => availableEncoders[e]);
    if (p === "vaapi" && !vaapiDeviceOk) ok = false;
    if (!ok) {
      opt.disabled = true;
      if (!opt.textContent.includes("（不可用）")) {
        opt.textContent += "（不可用）";
      }
    }
  }
}

// ==================== 转码平台切换 ====================
// keepPreset=true 时不重置 preset（用于页面加载后重新校验的调用）
function onPlatformChange(keepPreset) {
  const platform = $("platform").value;
  const formatSelect = $("format");
  const qualityLabel = $("qualityLabel");
  const qualityHint = $("qualityHint");
  const presetSelect = $("preset");
  const presetLabel = $("presetLabel");
  const qualityValue = $("qualityValue");
  const bitrateInput = $("bitrate");
  const scaleInput = $("scale");
  const platformHint = $("platformHint");
  const qualityModeSel = $("qualityMode");

  // 先恢复所有
  qualityValue.disabled = false;
  bitrateInput.disabled = false;
  presetSelect.disabled = false;
  scaleInput.disabled = false;
  formatSelect.disabled = false;
  qualityModeSel.disabled = false;
  platformHint.textContent = "";

  // === copy ===
  if (platform === "copy") {
    qualityLabel.textContent = "质量";
    qualityHint.textContent = "copy 模式无需设置";
    qualityValue.disabled = true;
    bitrateInput.disabled = true;
    presetSelect.disabled = true;
    scaleInput.disabled = true;
    formatSelect.disabled = true;
    qualityModeSel.disabled = true;
    $("bitrateGroup").style.display = "none";
    platformHint.textContent = "只做剪切/封装，速度极快；copy 按关键帧对齐，剪辑起点可能有几秒偏差，需要精确请改用转码";
    updateOutputPath();
    return;
  }

  // === smart ===
  if (platform === "smart") {
    qualityLabel.textContent = "CRF 质量";
    qualityHint.textContent = "由智能算法决定";
    qualityValue.disabled = true;
    bitrateInput.disabled = true;
    presetSelect.disabled = true;
    // scale 保留可用（缩放是独立策略）
    formatSelect.disabled = true;
    qualityModeSel.disabled = true;
    $("bitrateGroup").style.display = "none";

    const gpu = pickGpuPlatform();
    if (gpu) {
      platformHint.textContent = `按每个源自动分析，优先使用 ${GPU_NAMES[gpu]}，回退 CPU。适合批量`;
    } else {
      platformHint.textContent = "按每个源自动分析，使用 CPU 编码。适合批量";
    }
    updateOutputPath();
    return;
  }

  // === 手动平台 ===
  formatSelect.querySelector('option[value="av1"]').disabled = false;

  if (platform === "software") {
    qualityLabel.textContent = "CRF 质量";
    qualityHint.textContent = "越小越好，18-28 常用";
    presetLabel.textContent = "Preset";
    if (!keepPreset) presetSelect.value = "medium";
    enablePresets(["ultrafast", "fast", "medium", "slow", "veryslow"]);
    formatSelect.querySelector('option[value="av1"]').disabled = !availableEncoders["libsvtav1"];
    platformHint.textContent = "CPU 编码，速度慢但画质和压缩率最好";
  } else if (platform === "nvenc") {
    qualityLabel.textContent = "CQ 质量";
    qualityHint.textContent = "NVENC 恒定质量，推荐 20-28";
    presetLabel.textContent = "Preset (p1-p7)";
    if (!keepPreset) presetSelect.value = "p4";
    enablePresets(["p1", "p2", "p3", "p4", "p5", "p6", "p7"]);
    formatSelect.querySelector('option[value="av1"]').disabled = !availableEncoders["av1_nvenc"];
    platformHint.textContent = "NVIDIA 显卡硬件编码，速度极快";
  } else if (platform === "qsv") {
    qualityLabel.textContent = "ICQ 质量";
    qualityHint.textContent = "QSV 智能质量，推荐 22-28";
    presetLabel.textContent = "Preset";
    if (!keepPreset) presetSelect.value = "medium";
    enablePresets(["veryfast", "faster", "fast", "medium", "slow", "slower"]);
    formatSelect.querySelector('option[value="av1"]').disabled = !availableEncoders["av1_qsv"];
    platformHint.textContent = "Intel 核显硬件编码，低功耗";
  } else if (platform === "amf") {
    qualityLabel.textContent = "QP 质量";
    qualityHint.textContent = "AMF 恒定 QP，推荐 22-26";
    presetLabel.textContent = "Preset";
    if (!keepPreset) presetSelect.value = "balanced";
    enablePresets(["speed", "balanced", "quality"]);
    formatSelect.querySelector('option[value="av1"]').disabled = true;
    if (formatSelect.value === "av1") formatSelect.value = "h264";
    platformHint.textContent = "AMD 显卡硬件编码";
  } else if (platform === "vaapi") {
    qualityLabel.textContent = "QP 质量";
    qualityHint.textContent = "VAAPI 恒定 QP，推荐 22-26";
    presetLabel.textContent = "Preset";
    presetSelect.disabled = true;  // VAAPI 没有 preset 概念
    formatSelect.querySelector('option[value="av1"]').disabled = !availableEncoders["av1_vaapi"];
    platformHint.textContent = "Linux 通用硬件加速接口（无 preset 设置）";
  }

  onQualityModeChange();
  updateOutputPath();
}

function enablePresets(list) {
  const presetSelect = $("preset");
  let firstEnabled = null;
  for (const opt of presetSelect.options) {
    const ok = list.includes(opt.value);
    opt.disabled = !ok;
    opt.style.display = ok ? "" : "none";
    if (ok && firstEnabled === null) firstEnabled = opt.value;
  }
  if (!list.includes(presetSelect.value) && firstEnabled) {
    presetSelect.value = firstEnabled;
  }
}

function onQualityModeChange() {
  const platform = $("platform").value;
  const mode = $("qualityMode").value;
  const bg = $("bitrateGroup");
  const qv = $("qualityValue");
  const br = $("bitrate");
  const presetSelect = $("preset");
  const formatSelect = $("format");
  const scaleInput = $("scale");

  if (platform === "copy" || platform === "smart") {
    bg.style.display = "none";
    qv.disabled = true;
    br.disabled = true;
    presetSelect.disabled = true;
    formatSelect.disabled = true;
    return;
  }

  if (mode === "bitrate") {
    bg.style.display = "block";
    qv.disabled = true;
    br.disabled = false;
  } else {
    bg.style.display = "none";
    qv.disabled = false;
    br.disabled = true;
  }
}

// ==================== 参数记忆（localStorage） ====================
function saveSettings() {
  const data = { clipMode: clipMode() };
  SETTING_IDS.forEach(id => {
    const el = $(id);
    if (el) data[id] = el.value;
  });
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(data)); } catch (e) {}
}

function loadSettings() {
  let data = {};
  try { data = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") || {}; }
  catch (e) { data = {}; }

  const platformSel = $("platform");
  if (data.platform && Array.from(platformSel.options).some(o => o.value === data.platform)) {
    platformSel.value = data.platform;
  }
  onPlatformChange();

  ["format", "qualityMode", "qualityValue", "bitrate", "preset",
   "scale", "acodec", "abitrate", "extra", "headTrim", "tailTrim"].forEach(id => {
    if (data[id] == null) return;
    const el = $(id);
    if (!el) return;
    if (el.tagName === "SELECT") {
      const opt = Array.from(el.options).find(o => o.value === data[id]);
      if (!opt || opt.disabled) return;
    }
    el.value = data[id];
  });

  if (data.clipMode === "batch" || data.clipMode === "single") setClipMode(data.clipMode);

  onQualityModeChange();

  SETTING_IDS.forEach(id => {
    const el = $(id);
    if (!el) return;
    ["change", "input"].forEach(ev => el.addEventListener(ev, saveSettings));
  });
}

// ==================== 智能推荐 ====================
function pickGpuPlatform() {
  const has = (arr) => arr.some(e => availableEncoders[e]);
  if (has(["hevc_nvenc", "h264_nvenc"])) return "nvenc";
  if (has(["hevc_qsv", "h264_qsv"])) return "qsv";
  if (has(["hevc_amf", "h264_amf"])) return "amf";
  if (vaapiDeviceOk && has(["hevc_vaapi", "h264_vaapi"])) return "vaapi";
  return null;
}

const GPU_NAMES = {
  nvenc: "NVIDIA NVENC",
  qsv: "Intel QSV",
  amf: "AMD AMF",
  vaapi: "VAAPI",
};

const GPU_QUALITY_OFFSET = { nvenc: -2, qsv: -1, amf: -2, vaapi: -2 };
const GPU_DEFAULT_PRESET = { nvenc: "p5", qsv: "medium", amf: "balanced", vaapi: "medium" };

function recommendSettings(info) {
  if (!info || info.error) return null;
  const v = (info.video && info.video[0]) || {};
  const srcCodec = (v.codec || "").toLowerCase();
  const width = parseInt(v.width || 0);

  let srcBitrate = parseInt(v.bit_rate || 0);
  if (!srcBitrate && info.format && info.format.bit_rate) {
    srcBitrate = parseInt(info.format.bit_rate);
  }
  const srcKbps = Math.round(srcBitrate / 1000);
  if (!srcKbps || srcKbps < 50) return null;

  let resBonus = 0;
  if (width >= 3840) resBonus = 2;
  else if (width >= 1920) resBonus = 1;
  else if (width < 1280) resBonus = -1;

  const MODERN = ["hevc", "h265", "av1", "vp9"];
  const OLD = ["mpeg4", "mpeg2video", "msmpeg4", "wmv", "flv", "h263", "mpeg1video", "wmv3"];
  const isModern = MODERN.includes(srcCodec);
  const isOld = OLD.includes(srcCodec);

  const targetKbps = Math.max(200, Math.round(srcKbps * 0.65));

  let baseCrf;
  if (srcKbps < 800) baseCrf = 30;
  else if (srcKbps < 1500) baseCrf = 29;
  else if (srcKbps < 2500) baseCrf = 28;
  else if (srcKbps < 4000) baseCrf = 27;
  else if (srcKbps < 7000) baseCrf = 25;
  else if (srcKbps < 12000) baseCrf = 23;
  else baseCrf = 21;

  baseCrf += resBonus;
  if (isOld) baseCrf -= 1;
  if (isModern) baseCrf += 3;

  const crf = Math.max(18, Math.min(35, baseCrf));
  const preset = srcKbps > 4000 ? "slow" : "medium";

  const gpuPlatform = pickGpuPlatform();
  const gpuQuality = gpuPlatform
    ? Math.max(18, crf + (GPU_QUALITY_OFFSET[gpuPlatform] || 0))
    : null;
  const gpuPreset = gpuPlatform ? GPU_DEFAULT_PRESET[gpuPlatform] : null;

  const srcCodecDisp = v.codec ? v.codec.toUpperCase() : "?";
  const sizeRatio = Math.round((targetKbps / srcKbps) * 100);

  let mode = "crf";
  let reason, hint;

  if (isModern) {
    mode = "bitrate";
    reason = `源已是 ${srcCodecDisp}（${srcKbps} kbps），重编码容易膨胀`;
    hint = `建议目标码率 ${targetKbps}k，约为源的 ${sizeRatio}%`;
  } else {
    reason = `源 ${srcCodecDisp} ${srcKbps} kbps · 建议 H.265`;
    if (gpuPlatform) {
      hint = `CPU CRF ${crf} · ${GPU_NAMES[gpuPlatform]} CQ ${gpuQuality}`;
    } else {
      hint = `CRF ${crf} + preset ${preset}（未检测到可用 GPU）`;
    }
  }

  return {
    mode, crf, preset, bitrate: targetKbps + "k",
    srcKbps, targetKbps, isModern, isOld,
    reason, hint, gpuPlatform, gpuQuality, gpuPreset,
  };
}

// 根据源信息解析智能模式的实际参数
function resolveSmartParams(srcInfo) {
  const rec = recommendSettings(srcInfo);
  if (!rec) {
    // 拿不到源信息，退回默认
    return {
      platform: "software",
      format: "h265",
      quality_value: "23",
      bitrate: "",
      preset: "medium",
    };
  }
  // 优先用 GPU
  if (rec.gpuPlatform && rec.mode !== "bitrate") {
    return {
      platform: rec.gpuPlatform,
      format: "h265",
      quality_value: String(rec.gpuQuality),
      bitrate: "",
      preset: rec.gpuPreset,
    };
  }
  // 其他情况用 CPU
  return {
    platform: "software",
    format: "h265",
    quality_value: String(rec.crf),
    bitrate: rec.mode === "bitrate" ? rec.bitrate : "",
    preset: rec.preset,
  };
}

function renderRecommendation(info) {
  const box = $("recommendBox");
  const title = $("recommendTitle");
  const hint = $("recommendHint");
  const btnCpu = $("applyCpuBtn");
  const btnGpu = $("applyGpuBtn");

  lastRecommendation = recommendSettings(info);
  if (!lastRecommendation) {
    box.style.display = "none";
    return;
  }

  const rec = lastRecommendation;
  box.style.display = "flex";
  title.textContent = rec.reason;
  hint.textContent = rec.hint;

  if (rec.gpuPlatform && rec.mode !== "bitrate") {
    btnGpu.style.display = "inline-block";
    btnGpu.textContent = `${GPU_NAMES[rec.gpuPlatform]}（快）`;
  } else {
    btnGpu.style.display = "none";
  }
  btnCpu.textContent = rec.mode === "bitrate" ? "应用" : "CPU（慢）";
}

function applyRecommendation(mode) {
  if (!lastRecommendation) return;
  const rec = lastRecommendation;

  let platform, qualityValue, preset;
  if (rec.mode === "bitrate" || mode === "cpu" || !rec.gpuPlatform) {
    platform = "software";
    qualityValue = rec.crf;
    preset = rec.preset;
  } else {
    platform = rec.gpuPlatform;
    qualityValue = rec.gpuQuality;
    preset = rec.gpuPreset;
  }

  $("platform").value = platform;
  $("format").value = "h265";
  onPlatformChange();

  if (rec.mode === "bitrate") {
    $("qualityMode").value = "bitrate";
    $("bitrate").value = rec.bitrate;
    $("qualityValue").value = qualityValue;
  } else {
    $("qualityMode").value = "quality";
    $("qualityValue").value = qualityValue;
    $("bitrate").value = "";
  }
  onQualityModeChange();
  $("preset").value = preset;

  updateOutputPath();
  saveSettings();

  const btn = (mode === "gpu")
    ? $("applyGpuBtn")
    : $("applyCpuBtn");
  if (btn) {
    const orig = btn.textContent;
    btn.textContent = "✓ 已应用";
    setTimeout(() => { btn.textContent = orig; }, 1200);
  }
}

// ==================== 输出路径智能生成 ====================
// 是否设置了剪辑（按模式只算所属模式的时间：单文件=起始/结束，批量=片头/片尾）
function isClipping(mode) {
  if ((mode || clipMode()) === "batch") {
    const h = $("headTrim").value.trim();
    const t = $("tailTrim").value.trim();
    return (h && h !== "00:00:00") || (t && t !== "00:00:00");
  }
  const s = $("startTime").value.trim();
  const e = $("endTime").value.trim();
  return (s && s !== "00:00:00") || !!e;
}

function isTranscoding() {
  return $("platform").value !== "copy";
}

function currentCodecTag() {
  const p = $("platform").value;
  if (p === "smart") return "h265";  // 智能模式默认输出 H.265
  return ($("format").value || "h264").toLowerCase();
}

function calcSuffix(sameDir, mode) {
  const clip = isClipping(mode);
  const trans = isTranscoding();
  const codec = currentCodecTag();
  if (clip && trans) return "_cut_" + codec;
  if (clip) return "_cut";
  if (trans) return sameDir ? "_" + codec : "";
  return "";
}

function pickOutputExt(inputExt) {
  if (!isTranscoding()) return inputExt;   // 不转码（copy）保持原容器
  // 重新编码统一输出 mp4（h264/h265/av1 都能封装；音频不兼容时服务端会自动转 AAC）
  return ".mp4";
}

function computeOutputPath(inputPath, mode) {
  if (!inputPath) return "";
  const { dir, base, ext } = splitPath(inputPath);
  const suffix = calcSuffix(true, mode);
  const outExt = pickOutputExt(ext);
  return joinPath(dir, base + suffix + outExt);
}

// 用户没手动改过输出路径时，按当前输入自动生成（手动改过则保持不动）
function updateOutputPath() {
  if (outputManuallyEdited) return;
  const inputPath = $("inputPath").value;
  if (!inputPath) return;
  $("outputPath").value = computeOutputPath(inputPath);
}

function onTimeChanged() {
  updateClipUI();
  updateOutputPath();
}

$("outputPath").addEventListener("input", () => {
  outputManuallyEdited = true;
});

// 批量裁剪输入格式化
function setupTrimInput(id) {
  const el = $(id);
  el.addEventListener("blur", function () {
    const v = this.value.trim();
    this.value = v === "" ? "00:00:00" : formatTime(parseTime(v));
    updateOutputPath();
  });
  el.addEventListener("keydown", e => {
    if (e.key === "Enter") e.target.blur();
  });
  el.addEventListener("input", () => {
    updateOutputPath();
  });
}
setupTrimInput("headTrim");
setupTrimInput("tailTrim");

// ==================== 刷新恢复（任务状态持久化） ====================
// 服务端任务不受页面刷新影响，这里把界面状态存进 localStorage，刷新后重新接管
function isTerminalStatus(s) { return ["completed", "failed", "cancelled"].includes(s); }

function saveActiveTask(taskId, input, output) {
  try { localStorage.setItem(ACTIVE_TASK_KEY, JSON.stringify({ taskId, input, output })); }
  catch (e) {}
}
function loadActiveTask() {
  try { return JSON.parse(localStorage.getItem(ACTIVE_TASK_KEY) || "null"); }
  catch (e) { return null; }
}
function clearActiveTask() {
  try { localStorage.removeItem(ACTIVE_TASK_KEY); } catch (e) {}
}
function saveBatchState() {
  try {
    localStorage.setItem(BATCH_STATE_KEY, JSON.stringify({
      running: isBatchRunning, cancelled: batchCancelled, queue: batchQueue,
    }));
  } catch (e) {}
}
function loadBatchState() {
  try {
    const s = JSON.parse(localStorage.getItem(BATCH_STATE_KEY) || "null");
    return s && Array.isArray(s.queue) ? s : null;
  } catch (e) { return null; }
}

// 按输出路径找回服务端任务（提交请求的过程中刷新时用）
async function findTaskByOutput(outputPath) {
  try {
    const r = await fetch("/api/tasks");
    if (!r.ok) return null;
    const d = await r.json();
    const norm = p => (p || "").replace(/\\/g, "/").toLowerCase();
    const list = (d.tasks || []).filter(t => norm(t.output) === norm(outputPath));
    return list.length ? list[list.length - 1] : null;
  } catch (e) { return null; }
}

// 刷新后恢复：先恢复批量队列（进行中则继续执行），再恢复单文件任务
async function restoreSession() {
  const saved = loadBatchState();
  if (saved && saved.queue.length > 0) {
    batchQueue = saved.queue;
    batchTotal = batchQueue.length;
    batchDone = batchQueue.filter(it => isTerminalStatus(it.status)).length;
    renderQueue();
    if (saved.running && saved.cancelled) {
      // 刷新前已点过「中止」：把还没结束的项直接标记为已中止
      batchQueue.forEach(it => { if (!isTerminalStatus(it.status)) it.status = "cancelled"; });
      batchDone = batchQueue.filter(it => isTerminalStatus(it.status)).length;
      saveBatchState();
      renderQueue();
    } else if (saved.running) {
      runBatch({ resume: true });
    }
  } else {
    renderQueue();
  }
  if (!isBatchRunning) await restoreSingleTask();
}

// 刷新后恢复单文件任务：还在运行就继续接管，已结束就还原结果
async function restoreSingleTask() {
  const saved = loadActiveTask();
  if (!saved || !saved.taskId) return;

  let t;
  try {
    const r = await fetch("/api/task/" + saved.taskId);
    if (!r.ok) { clearActiveTask(); return; }   // 任务已被清理（或服务已重启）
    t = await r.json();
  } catch (e) { return; }   // 服务器暂时不可达：保留状态，下次刷新再试

  if (saved.input) $("inputPath").value = saved.input;
  if (saved.output) {
    $("outputPath").value = saved.output;
    outputManuallyEdited = true;
  }

  if (t.status === "pending" || t.status === "running") {
    // 任务还在跑：恢复界面并继续等待
    currentTaskId = saved.taskId;
    const btn = $("submitBtn");
    const stopBtn = $("stopBtn");
    btn.disabled = true;
    btn.textContent = "运行中…";
    stopBtn.style.display = "inline-block";
    stopBtn.disabled = false;
    $("progressWrap").style.display = "block";
    $("logBox").style.display = "block";
    updateProgressUI(t);
    const done = await waitForTask(saved.taskId);
    currentTaskId = null;
    finishSingleTask(done);
  } else {
    finishSingleTask(t, true);
  }
}

// ==================== 批量队列 ====================
function renderQueue() {
  const ul = $("queueList");
  const countEl = $("queueCount");
  const hintEl = $("queueHint");

  countEl.textContent = batchQueue.length;
  ul.innerHTML = "";

  if (batchQueue.length === 0) {
    hintEl.style.display = "block";
    $("queueFooter").style.display = "none";
    return;
  }
  hintEl.style.display = "none";

  batchQueue.forEach((item, idx) => {
    const st = STATUS_MAP[item.status || "pending"] || STATUS_MAP.pending;

    const li = document.createElement("li");
    li.className = "queue-item";
    li.dataset.status = item.status || "pending";
    li.title = item.path + (item.error ? "\n错误: " + item.error : "");

    li.innerHTML = `
      <span class="q-num">${idx + 1}</span>
      <span class="q-status-icon">${st.icon}</span>
      <div class="q-info">
        <span class="q-name"></span>
        <span class="q-status-text"></span>
      </div>
      <button class="q-remove" title="移除">×</button>
    `;
    li.querySelector(".q-name").textContent = item.name;

    const statusParts = [st.text];
    if (item.outName) statusParts.push("→ " + item.outName);
    if (item.error) statusParts.push("错误: " + String(item.error).slice(0, 40));
    li.querySelector(".q-status-text").textContent = statusParts.join(" · ");

    li.querySelector(".q-remove").onclick = (e) => {
      e.stopPropagation();
      removeFromQueue(idx);
    };

    if (!isBatchRunning) {
      li.onclick = (e) => {
        if (e.target.classList.contains("q-remove")) return;
        previewFile(item.path);
      };
    }

    ul.appendChild(li);
  });

  // 底部进度
  if (isBatchRunning) {
    $("queueFooter").style.display = "block";
    const pct = batchTotal > 0 ? (batchDone / batchTotal) * 100 : 0;
    $("queueProgressBar").style.width = pct + "%";
    $("queueProgressText").textContent =
      `${batchDone} / ${batchTotal}（${pct.toFixed(0)}%）`;
  } else if (batchTotal > 0 && batchDone === batchTotal) {
    $("queueFooter").style.display = "block";
    $("queueProgressBar").style.width = "100%";
    $("queueProgressText").textContent =
      `已完成 ${batchTotal} 个`;
  }
}

function addToQueue(path, name) {
  if (!path) return;
  if (batchQueue.some(it => it.path === path)) return;
  batchQueue.push({
    path,
    name: name || splitPath(path).filename,
    status: "pending",
    taskId: null,
    error: "",
  });
  saveBatchState();
  renderQueue();
}

function removeFromQueue(idx) {
  if (isBatchRunning) {
    toast("批量处理中，无法移除队列项", "warn");
    return;
  }
  batchQueue.splice(idx, 1);
  saveBatchState();
  renderQueue();
  // 同步左侧按钮状态
  refreshFileListAddButtons();
}

function clearQueue() {
  if (isBatchRunning) {
    toast("批量处理中，无法清空队列", "warn");
    return;
  }
  if (batchQueue.length === 0) return;
  if (!confirm(`确定清空队列中的 ${batchQueue.length} 个文件吗？`)) return;
  batchQueue = [];
  batchTotal = 0;
  batchDone = 0;
  saveBatchState();
  renderQueue();
  refreshFileListAddButtons();
}

function clearCompleted() {
  if (isBatchRunning) {
    toast("批量处理中，无法清空队列", "warn");
    return;
  }
  const before = batchQueue.length;
  batchQueue = batchQueue.filter(it => it.status === "pending" || it.status === "running");
  if (batchQueue.length === before) {
    toast("没有已完成的项目", "warn");
    return;
  }
  saveBatchState();
  renderQueue();
  refreshFileListAddButtons();
}

// 把失败 / 中止的项目重置为待处理，便于一键重跑
function retryFailed() {
  if (isBatchRunning) {
    toast("批量处理中，无法重试", "warn");
    return;
  }
  let n = 0;
  batchQueue.forEach(it => {
    if (it.status === "failed" || it.status === "cancelled") {
      it.status = "pending";
      it.error = "";
      it.taskId = null;
      it.outName = "";
      n++;
    }
  });
  if (!n) {
    toast("没有失败或中止的项目", "warn");
    return;
  }
  saveBatchState();
  renderQueue();
  toast(`已把 ${n} 个项目重置为待处理`, "ok");
}

function refreshFileListAddButtons() {
  document.querySelectorAll("#filelist li").forEach(li => {
    const path = li.dataset.path;
    if (!path) return;
    const btn = li.querySelector(".add-btn");
    if (!btn) return;
    const inQueue = batchQueue.some(q => q.path === path);
    if (inQueue) {
      btn.classList.add("added");
      btn.textContent = "✓";
      btn.title = "已在队列";
    } else {
      btn.classList.remove("added");
      btn.textContent = "+";
      btn.title = "加入队列";
    }
  });
}

function updateQueueItem(idx, patch) {
  if (idx < 0 || idx >= batchQueue.length) return;
  Object.assign(batchQueue[idx], patch);
  saveBatchState();
  renderQueue();
}

// 读取当前 UI 的编码参数（overrides 可覆盖平台/格式/质量/码率/preset，用于智能模式）
function collectEncoderParams(overrides) {
  const ov = overrides || {};
  const mode = $("qualityMode").value;
  const platform = ov.platform || $("platform").value;
  const autoBitrate = platform !== "copy" && platform !== "smart" && mode === "bitrate";
  return {
    platform: platform,
    format: ov.format || $("format").value,
    quality_value: ov.quality_value ?? $("qualityValue").value,
    bitrate: "bitrate" in ov ? ov.bitrate : (autoBitrate ? $("bitrate").value : ""),
    preset: ov.preset || $("preset").value,
    scale: $("scale").value,
    acodec: $("acodec").value,
    abitrate: $("abitrate").value,
    extra: $("extra").value,
  };
}

// 构建 /api/transcode 请求体；trim 可选（"batch" 用批量字段），默认按当前剪辑模式
function buildPayload(inputPath, outputPath, opts) {
  opts = opts || {};
  const batch = (opts.trim || clipMode()) === "batch";
  const timing = batch
    ? { start_time: "", end_time: "", head_trim: $("headTrim").value.trim(), tail_trim: $("tailTrim").value.trim() }
    : { start_time: $("startTime").value, end_time: $("endTime").value, head_trim: "", tail_trim: "" };
  return Object.assign({ input: inputPath, output: outputPath }, timing, collectEncoderParams(opts.params));
}

// ==================== 收藏夹 / 输出目录（localStorage 列表，逻辑共用） ====================
function lsGet(key) {
  try { return JSON.parse(localStorage.getItem(key) || "[]"); }
  catch (e) { return []; }
}
function lsSave(key, list) { localStorage.setItem(key, JSON.stringify(list)); }

// 取路径最后一段作为显示名（如 "C:\videos" -> "videos"）
function pathBaseName(p) {
  const segments = p.split(/[\/\\]/).filter(Boolean);
  return segments.length > 0 ? segments[segments.length - 1] : p;
}

// 通用收藏列表渲染：点击条目 = onOpen(path)，点 × = onRemove(idx)
function renderFavList(key, listId, emptyText, onOpen, onRemove) {
  const list = lsGet(key);
  const el = $(listId);
  el.innerHTML = "";
  if (list.length === 0) {
    el.innerHTML = `<span class="fav-empty">${emptyText}</span>`;
    return;
  }
  list.forEach((fav, idx) => {
    const item = document.createElement("div");
    item.className = "fav-item";
    item.title = fav.path;
    item.innerHTML = `<span class="fav-name"></span>
      <button class="fav-remove" title="移除">×</button>`;
    item.querySelector(".fav-name").textContent = fav.name;
    item.querySelector(".fav-name").onclick = (e) => {
      e.stopPropagation();
      onOpen(fav.path);
    };
    item.querySelector(".fav-remove").onclick = (e) => {
      e.stopPropagation();
      onRemove(idx);
    };
    el.appendChild(item);
  });
}

// 去重后追加到列表；已存在时提示并返回 false
function addToList(key, path, dupMsg) {
  const list = lsGet(key);
  if (list.some(f => f.path === path)) { toast(dupMsg, "warn"); return false; }
  list.push({ name: pathBaseName(path), path });
  lsSave(key, list);
  return true;
}

function removeFromList(key, idx, renderFn) {
  const list = lsGet(key);
  list.splice(idx, 1);
  lsSave(key, list);
  renderFn();
}

function renderFavorites() {
  renderFavList(FAV_KEY, "favList", "点右上 + 收藏当前目录",
    path => loadPath(path), idx => removeFavorite(idx));
}
function addCurrentToFavorites() {
  if (!currentDir) return;
  if (addToList(FAV_KEY, currentDir, "这个目录已经在收藏夹里了")) renderFavorites();
}
function removeFavorite(idx) { removeFromList(FAV_KEY, idx, renderFavorites); }

// ==================== 输出目录收藏 ====================
function renderOutputDirs() {
  renderFavList(OUT_DIR_KEY, "outputDirList", "点右上 + 收藏当前输出目录",
    path => applyOutputDir(path), idx => removeOutputDir(idx));
}
function addCurrentOutputDir() {
  const outPath = $("outputPath").value.trim();
  if (!outPath) { toast("输出文件路径为空", "warn"); return; }
  const { dir } = splitPath(outPath);
  if (!dir) { toast("无法解析目录", "warn"); return; }
  if (addToList(OUT_DIR_KEY, dir, "这个目录已经在输出目录列表里了")) renderOutputDirs();
}
function removeOutputDir(idx) { removeFromList(OUT_DIR_KEY, idx, renderOutputDirs); }

function applyOutputDir(newDir) {
  const inputPath = $("inputPath").value.trim();
  if (!inputPath) { toast("请先选择输入文件", "warn"); return; }

  const { dir: inputDir, base, ext } = splitPath(inputPath);
  const norm = (p) => p.replace(/[\/\\]+$/, "").replace(/\\/g, "/").toLowerCase();
  const sameDir = norm(newDir) === norm(inputDir);

  const suffix = calcSuffix(sameDir);
  const outExt = pickOutputExt(ext);
  const filename = base + suffix + outExt;
  $("outputPath").value = joinPath(newDir, filename);
  outputManuallyEdited = true;
}

// ==================== 时间工具 ====================
function formatTime(seconds) {
  if (!seconds || seconds < 0 || !isFinite(seconds)) seconds = 0;
  seconds = Math.floor(seconds);
  const h = String(Math.floor(seconds / 3600)).padStart(2, "0");
  const m = String(Math.floor((seconds % 3600) / 60)).padStart(2, "0");
  const s = String(seconds % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
}
function parseTime(str) {
  if (!str) return 0;
  str = String(str).trim();
  if (str === "") return 0;
  const parts = str.split(":").map(p => p.trim());
  if (parts.length === 3) {
    return (parseInt(parts[0]) || 0) * 3600 + (parseInt(parts[1]) || 0) * 60 + (parseFloat(parts[2]) || 0);
  } else if (parts.length === 2) {
    return (parseInt(parts[0]) || 0) * 60 + (parseFloat(parts[1]) || 0);
  }
  return parseFloat(str) || 0;
}

// ==================== 播放器 ====================
const player = $("player");
const videoPlaceholder = $("videoPlaceholder");
const videoArea = $("videoArea");

player.addEventListener("loadedmetadata", () => {
  videoDuration = player.duration || 0;
  videoPlaceholder.style.display = "none";
  $("endTime").placeholder = formatTime(videoDuration);
  if (player.videoWidth && player.videoHeight) {
    videoArea.style.aspectRatio = `${player.videoWidth} / ${player.videoHeight}`;
  }
  updateClipUI();
});
player.addEventListener("error", () => {
  videoPlaceholder.textContent = "⚠️ 无法加载该视频（浏览器可能不支持此编码）";
  videoPlaceholder.style.display = "flex";
  videoDuration = 0;
});

function setStartFromPlayer() {
  if (!player.src) return;
  $("startTime").value = formatTime(player.currentTime || 0);
  onTimeChanged();
}
function setEndFromPlayer() {
  if (!player.src) return;
  $("endTime").value = formatTime(player.currentTime || 0);
  onTimeChanged();
}

$("startTime").addEventListener("blur", function () {
  const v = this.value.trim();
  this.value = v === "" ? "00:00:00" : formatTime(parseTime(v));
  onTimeChanged();
});
$("endTime").addEventListener("blur", function () {
  const v = this.value.trim();
  this.value = v === "" ? "" : formatTime(parseTime(v));
  onTimeChanged();
});
["startTime", "endTime"].forEach(id => {
  $(id).addEventListener("keydown", e => {
    if (e.key === "Enter") e.target.blur();
  });
  $(id).addEventListener("input", onTimeChanged);
});

function updateClipUI() {
  const s = parseTime($("startTime").value);
  const eRaw = $("endTime").value.trim();
  const e = eRaw ? parseTime(eRaw) : (videoDuration || 0);

  $("clipStartTip").textContent = formatTime(s);
  $("clipEndTip").textContent =
    eRaw ? formatTime(e) : (videoDuration ? formatTime(videoDuration) : "--:--:--");

  const bar = $("clipBarActive");
  if (videoDuration > 0) {
    const leftPct = Math.min(100, Math.max(0, (s / videoDuration) * 100));
    const rightPct = Math.min(100, Math.max(0, 100 - (e / videoDuration) * 100));
    bar.style.left = leftPct + "%";
    bar.style.right = rightPct + "%";
  } else {
    bar.style.left = "0%";
    bar.style.right = "0%";
  }

  const hint = $("durationHint");
  let dur = 0;
  if (e > s) dur = e - s;
  else if (videoDuration > 0) dur = videoDuration - s;
  hint.textContent = dur > 0 ? `剪辑时长：${formatTime(dur)}` : "剪辑时长：--";
}

// ==================== 剪辑模式（单文件精确剪辑 / 批量裁剪） ====================
function clipMode() {
  const el = document.querySelector('input[name="clipMode"]:checked');
  return el && el.value === "batch" ? "batch" : "single";
}

// 切换模式：时间输入区整体切换；两种模式的行结构样式一致
function setClipMode(mode) {
  const batch = mode === "batch";
  const radio = document.querySelector(`input[name="clipMode"][value="${mode}"]`);
  if (radio) radio.checked = true;
  $("singleClipBox").style.display = batch ? "none" : "";
  $("batchClipBox").style.display = batch ? "" : "none";
  $("clipBar").style.display = batch ? "none" : "";
  document.querySelector(".clip-time-tip").style.display = batch ? "none" : "";
  updateClipUI();
  updateOutputPath();   // 输出名后缀按当前模式的时间规则生成
}

// 批量裁剪「清零」
function resetTrim(id) {
  $(id).value = "00:00:00";
  onTimeChanged();
}

document.querySelectorAll('input[name="clipMode"]').forEach(radio => {
  radio.addEventListener("change", () => {
    setClipMode(radio.value);
    saveSettings();   // 记住选择的模式
  });
});

// ==================== 文件浏览 ====================
async function loadPath(path) {
  if (!path) return;
  const seq = ++viewSeq;   // 导航序号：晚发起的导航优先，过期响应直接丢弃
  try {
    const r = await fetch("/api/browse?path=" + encodeURIComponent(path));
    const d = await r.json();
    if (seq !== viewSeq) return;   // 期间又发生了新导航，忽略这次过期结果
    if (d.error) { toast(d.error, "error"); return; }
    currentDir = d.path;
    browseMode = "browse";
    $("pathInput").value = d.path;

    const ul = $("filelist");
    ul.innerHTML = "";

    if (d.parent) {
      const li = document.createElement("li");
      li.className = "up";
      li.innerHTML = `<span class="name">..</span>`;
      li.onclick = () => loadPath(d.parent);
      ul.appendChild(li);
    }

    if (d.items.length === 0) {
      // 注意：这里必须 createElement 追加。用 innerHTML += 会重建整个列表，
      // 把上面「..」行的 JS 点击事件一起丢掉（空目录里「..」就点不动了）
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = "（空目录，无视频文件）";
      ul.appendChild(li);
    }

    d.items.forEach(item => {
      if (item.is_dir) {
        const li = document.createElement("li");
        li.dataset.path = item.path;
        const size = item.size_human ? `<span class="size">${item.size_human}</span>` : "";
        li.innerHTML = `<span class="name"></span>${size}`;
        li.querySelector(".name").textContent = item.name;
        li.onclick = () => loadPath(item.path);
        ul.appendChild(li);
      } else {
        // 普通浏览：悬停显示完整路径
        ul.appendChild(buildVideoLi(item, item.path));
      }
    });

    rebuildExtFilter();
  } catch (e) {
    if (seq === viewSeq) toast("加载失败: " + e, "error");
  }
}

// 构建视频文件行（目录浏览 / 探查结果共用）
// hoverTip: 鼠标悬停提示（探查视图传相对路径，普通浏览传绝对路径）
function buildVideoLi(item, hoverTip) {
  const li = document.createElement("li");
  li.dataset.path = item.path;
  li.dataset.ext = splitPath(item.path).ext.replace(".", "").toLowerCase();
  li.title = hoverTip || item.path;
  const inQueue = batchQueue.some(q => q.path === item.path);
  li.innerHTML = `
    <input type="checkbox" class="chk" title="勾选后可批量加入队列（Shift 点击连选）">
    <span class="name"></span>
    <span class="size">${item.size_human || ""}</span>
    <button class="add-btn${inQueue ? " added" : ""}" title="${inQueue ? "已在队列" : "加入队列"}">${inQueue ? "✓" : "+"}</button>
  `;
  li.querySelector(".name").textContent = item.name;

  const chk = li.querySelector(".chk");
  chk.onclick = (e) => {
    e.stopPropagation();
    onRowCheckClick(chk, e.shiftKey);
  };

  const addBtn = li.querySelector(".add-btn");
  addBtn.onclick = (e) => {
    e.stopPropagation();
    if (batchQueue.some(q => q.path === item.path)) return;
    addToQueue(item.path, item.name);
    addBtn.classList.add("added");
    addBtn.textContent = "✓";
    addBtn.title = "已在队列";
  };

  li.onclick = (e) => {
    if (e.target.classList.contains("add-btn")) return;
    document.querySelectorAll("#filelist li").forEach(x => x.classList.remove("selected"));
    li.classList.add("selected");
    selectFile(item.path);
  };
  return li;
}

// ==================== 列表多选（按后缀筛选 / 全选反选 / Shift 连选 / 批量加入） ====================
let lastCheckIndex = -1;   // Shift 连选的锚点

// 可见（未被筛选隐藏）且带勾选框的行，按列表顺序
function getCheckRows() {
  return Array.from(document.querySelectorAll("#filelist li"))
    .filter(li => li.querySelector(".chk") && li.style.display !== "none");
}

function updateSelCount() {
  const checked = document.querySelectorAll("#filelist .chk:checked").length;
  const btn = $("addSelBtn");
  if (btn) {
    btn.textContent = `加入所选 (${checked})`;
    btn.disabled = checked === 0;
  }
  document.querySelectorAll("#filelist li").forEach(li => {
    const chk = li.querySelector(".chk");
    li.classList.toggle("picked", !!(chk && chk.checked));
  });
}

// 勾选框点击：Shift 点击 = 从锚点到当前行的范围连选
function onRowCheckClick(chk, shiftKey) {
  const rows = getCheckRows();
  const idx = rows.findIndex(li => li.querySelector(".chk") === chk);
  if (idx >= 0) {
    if (shiftKey && lastCheckIndex >= 0 && lastCheckIndex < rows.length) {
      const a = Math.min(lastCheckIndex, idx);
      const b = Math.max(lastCheckIndex, idx);
      for (let i = a; i <= b; i++) rows[i].querySelector(".chk").checked = chk.checked;
    }
    lastCheckIndex = idx;
  }
  updateSelCount();
}

function selectAllVisible() {
  getCheckRows().forEach(li => { li.querySelector(".chk").checked = true; });
  lastCheckIndex = -1;
  updateSelCount();
}

function invertSelection() {
  getCheckRows().forEach(li => {
    const chk = li.querySelector(".chk");
    chk.checked = !chk.checked;
  });
  lastCheckIndex = -1;
  updateSelCount();
}

function clearSelection() {
  document.querySelectorAll("#filelist .chk").forEach(chk => { chk.checked = false; });
  lastCheckIndex = -1;
  updateSelCount();
}

// 按后缀重建筛选下拉（保留仍可用的当前选择）
function rebuildExtFilter() {
  const sel = $("extFilter");
  if (!sel) return;
  const prev = sel.value;
  const exts = [...new Set(Array.from(document.querySelectorAll("#filelist li[data-ext]"))
    .map(li => li.dataset.ext).filter(Boolean))].sort();
  sel.innerHTML = '<option value="">全部后缀</option>' +
    exts.map(x => `<option value="${x}">.${x}</option>`).join("");
  if (exts.includes(prev)) sel.value = prev;
  applyExtFilter();
}

// 应用筛选：隐藏不匹配的视频文件（文件夹与「..」不受影响）
function applyExtFilter() {
  const sel = $("extFilter");
  const val = sel ? sel.value : "";
  let total = 0, shown = 0;
  document.querySelectorAll("#filelist li[data-ext]").forEach(li => {
    total++;
    const ok = !val || li.dataset.ext === val;
    li.style.display = ok ? "" : "none";
    if (ok) shown++;
  });
  const ul = $("filelist");
  const hint = ul.querySelector("li.filter-empty");
  if (total > 0 && shown === 0 && val) {
    if (!hint) {
      const li = document.createElement("li");
      li.className = "empty filter-empty";
      li.textContent = "（当前筛选没有匹配的文件）";
      ul.appendChild(li);
    }
  } else if (hint) {
    hint.remove();
  }
  lastCheckIndex = -1;
  updateSelCount();
}

// 把勾选的视频批量加入队列
function addSelectedToQueue() {
  const rows = Array.from(document.querySelectorAll("#filelist li"))
    .filter(li => li.querySelector(".chk:checked"));
  if (rows.length === 0) {
    toast("先勾选要加入的视频", "warn");
    return;
  }
  const before = batchQueue.length;
  rows.forEach(li => {
    if (li.dataset.path) addToQueue(li.dataset.path, li.querySelector(".name").textContent);
  });
  const added = batchQueue.length - before;
  rows.forEach(li => { li.querySelector(".chk").checked = false; });
  lastCheckIndex = -1;
  refreshFileListAddButtons();
  updateSelCount();
  const dup = rows.length - added;
  toast(`已加入 ${added} 个到批量队列` + (dup > 0 ? `（${dup} 个已在队列）` : ""), "ok");
}

// ==================== 文件夹选择弹窗 ====================
let pickerDir = "";
let pickerParent = null;
let pickerSeq = 0;   // 与 loadPath 同理：丢弃过期的加载结果

// Windows 盘符根目录（如 C:\）
function isWindowsDriveRoot(path) {
  return /^[A-Za-z]:[\\/]*$/.test(path || "");
}

// 选择器列表项：显示名字，点击进入对应目录（子目录列表与盘符列表共用）
function appendPickerItem(listEl, name, path) {
  const li = document.createElement("li");
  li.innerHTML = '<span class="name"></span>';
  li.querySelector(".name").textContent = name;
  li.onclick = () => loadPickerDir(path);
  listEl.appendChild(li);
}

// 按最后一次成功加载的位置，恢复「上级 / 选择此文件夹」可用状态
function syncPickerButtons() {
  const upBtn = $("folderUpBtn");
  const okBtn = $("folderConfirmBtn");
  if (upBtn) upBtn.disabled = !(pickerParent || isWindowsDriveRoot(pickerDir));
  if (okBtn) okBtn.disabled = !pickerDir;
}

async function openFolderPicker() {
  // 打开时先复位：加载完成前「上级/确认」不可点，避免点了没反应
  pickerDir = "";
  pickerParent = null;
  const upBtn = $("folderUpBtn");
  const okBtn = $("folderConfirmBtn");
  if (upBtn) upBtn.disabled = true;
  if (okBtn) okBtn.disabled = true;
  $("folderModal").style.display = "flex";
  await loadPickerDir(currentDir || window.MEDIA_DIR);
}

function closeFolderPicker() {
  $("folderModal").style.display = "none";
}

async function loadPickerDir(path) {
  const pathEl = $("folderModalPath");
  const listEl = $("folderModalList");
  const upBtn = $("folderUpBtn");
  const okBtn = $("folderConfirmBtn");
  const seq = ++pickerSeq;
  if (upBtn) upBtn.disabled = true;   // 加载中禁点，避免「上级」点了没反应
  if (okBtn) okBtn.disabled = true;
  listEl.innerHTML = '<li class="empty">加载中…</li>';
  try {
    const r = await fetch("/api/browse?path=" + encodeURIComponent(path));
    const d = await r.json();
    if (seq !== pickerSeq) return;   // 期间又有新的目录加载，忽略过期结果
    if (d.error) {
      toast(d.error, "error");
      listEl.innerHTML = '<li class="empty">加载失败</li>';
      syncPickerButtons();   // 仍可回到上一个有效位置
      return;
    }
    pickerDir = d.path;
    pickerParent = d.parent;
    pathEl.textContent = d.path;
    pathEl.title = d.path;
    syncPickerButtons();

    listEl.innerHTML = "";
    const dirs = d.items.filter(it => it.is_dir);
    if (dirs.length === 0) {
      listEl.innerHTML = '<li class="empty">（没有子文件夹）</li>';
      return;
    }
    dirs.forEach(it => appendPickerItem(listEl, it.name, it.path));
  } catch (e) {
    if (seq !== pickerSeq) return;
    toast("加载失败: " + e, "error");
    syncPickerButtons();
  }
}

// 「此电脑」视图：列出所有盘符（Windows）/ 根目录
async function showPickerDrives() {
  const pathEl = $("folderModalPath");
  const listEl = $("folderModalList");
  const upBtn = $("folderUpBtn");
  const okBtn = $("folderConfirmBtn");
  const seq = ++pickerSeq;
  pickerDir = "";
  pickerParent = null;
  upBtn.disabled = true;
  if (okBtn) okBtn.disabled = true;   // 「此电脑」本身不能作为目录选择
  pathEl.textContent = "此电脑（选择盘符）";
  pathEl.title = "";
  listEl.innerHTML = '<li class="empty">加载中…</li>';
  try {
    const r = await fetch("/api/drives");
    const d = await r.json();
    if (seq !== pickerSeq) return;
    listEl.innerHTML = "";
    const items = d.items || [];
    if (items.length === 0) {
      listEl.innerHTML = '<li class="empty">（没有可用盘符）</li>';
      return;
    }
    items.forEach(it => appendPickerItem(listEl, it.name, it.path));
  } catch (e) {
    if (seq === pickerSeq) toast("加载失败: " + e, "error");
  }
}

function pickerGoUp() {
  if (pickerParent) {
    loadPickerDir(pickerParent);
  } else if (isWindowsDriveRoot(pickerDir)) {
    showPickerDrives();
  }
}

function confirmFolderPicker() {
  if (!pickerDir) return;
  closeFolderPicker();
  loadPath(pickerDir);
}

// ==================== 探查（递归扫描视频文件） ====================
async function scanCurrentDir() {
  const dir = currentDir;
  const btn = $("scanBtn");
  const ul = $("filelist");
  const seq = ++viewSeq;   // 与 loadPath 共用导航序号
  btn.disabled = true;
  btn.textContent = "探查中…";
  ul.innerHTML = "";
  const loading = document.createElement("li");
  loading.className = "empty";
  loading.textContent = "正在探查 " + dir + " …";
  ul.appendChild(loading);

  try {
    const r = await fetch("/api/scan?path=" + encodeURIComponent(dir));
    const d = await r.json();
    if (seq !== viewSeq) return;   // 期间又发生了新导航，忽略过期结果
    if (d.error) {
      toast(d.error, "error");
      loadPath(dir);
      return;
    }
    browseMode = "scan";
    renderScanResult(d);
  } catch (e) {
    if (seq === viewSeq) {
      toast("探查失败: " + e, "error");
      loadPath(dir);
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "探查";
  }
}

function renderScanResult(d) {
  const ul = $("filelist");
  ul.innerHTML = "";

  const back = document.createElement("li");
  back.className = "up";
  back.title = "返回普通目录浏览";
  back.innerHTML = '<span class="name"></span>';
  back.querySelector(".name").textContent =
    `返回浏览（探查到 ${d.count} 个视频${d.truncated ? "，已达上限截断" : ""}）`;
  back.onclick = () => loadPath(currentDir);
  ul.appendChild(back);

  if (!d.items || d.items.length === 0) {
    const empty = document.createElement("li");
    empty.className = "empty";
    empty.textContent = "（没有找到视频文件）";
    ul.appendChild(empty);
    rebuildExtFilter();
    return;
  }

  // 子目录中的文件：相对路径只在鼠标悬停提示里显示，列表保持清爽
  d.items.forEach(item => {
    const tip = (item.rel && item.rel !== item.name) ? item.rel : item.path;
    ul.appendChild(buildVideoLi(item, tip));
  });
  rebuildExtFilter();
}

// 任务完成后按当前视图刷新（普通浏览 / 探查结果）
function refreshView() {
  if (browseMode === "scan") scanCurrentDir();
  else loadPath(currentDir);
}

async function previewFile(path) {
  $("inputPath").value = path;
  outputManuallyEdited = false;
  updateOutputPath();

  videoArea.style.aspectRatio = "16 / 9";
  videoPlaceholder.textContent = "⏳ 正在加载视频预览…";
  videoPlaceholder.style.display = "flex";
  player.src = "/api/stream?path=" + encodeURIComponent(path);
  player.load();

  const r = await fetch("/api/info?path=" + encodeURIComponent(path));
  const d = await r.json();
  currentSrcInfo = d;
  const box = $("infoBox");

  if (d.error) {
    box.style.display = "block";
    box.innerHTML = `<b style="color:#ff6b6b">读取视频信息失败：</b>
      <span style="color:#ccc">${d.error}</span>`;
    // 清掉上一个文件的智能推荐，避免误导
    lastRecommendation = null;
    $("recommendBox").style.display = "none";
    return;
  }

  const dur = parseFloat(d.format.duration || 0);
  const durStr = dur ? formatTime(dur) : "未知";
  const v = (d.video && d.video[0]) || {};
  const a = (d.audio && d.audio[0]) || {};
  const { filename } = splitPath(path);

  box.style.display = "block";
  box.innerHTML = `
    <b>文件：</b><span style="color:#ccc">${filename}</span><br>
    <b>容器：</b><span class="tag">${d.format.format_name || "?"}</span><br>
    <b>时长：</b>${durStr}<br>
    <b>视频：</b><span class="tag">${v.codec || "?"}</span> · ${v.width || "?"}×${v.height || "?"} · ${v.fps || "?"} fps${v.bit_rate ? " · " + Math.round(v.bit_rate / 1000) + " kbps" : ""}<br>
    <b>音频：</b><span class="tag">${a.codec || "无"}</span>${a.sample_rate ? " · " + a.sample_rate + " Hz" : ""}${a.channels ? " · " + a.channels + " ch" : ""}${a.bit_rate ? " · " + Math.round(a.bit_rate / 1000) + " kbps" : ""}
  `;

  renderRecommendation(d);
}

async function selectFile(path) {
  await previewFile(path);
  $("startTime").value = "00:00:00";
  $("endTime").value = "";
  videoDuration = 0;
  updateClipUI();
  outputManuallyEdited = false;
  updateOutputPath();
}

// ==================== 提交 / 中止 ====================
// 提交转码请求；输出文件已存在（409）时弹窗确认后带 overwrite 重试
async function postTranscode(payload) {
  const send = (body) => fetch("/api/transcode", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  let r = await send(payload);
  if (r.status === 409) {
    const d = await r.json();
    if (d.exists && confirm(`输出文件已存在：\n${payload.output}\n\n是否覆盖？`)) {
      payload.overwrite = true;
      r = await send(payload);
    } else {
      return { error: "输出文件已存在，已取消" };
    }
  }
  return await r.json();
}

// 轮询任务直到结束（网络抖动重试几次，避免误判失败）
function waitForTask(taskId) {
  return new Promise((resolve) => {
    let errors = 0;
    const check = async () => {
      try {
        const r = await fetch("/api/task/" + taskId);
        const t = await r.json();
        if (!r.ok) {
          resolve({ status: "failed", error: t.error || "任务不存在或已被清理" });
          return;
        }
        errors = 0;
        if (t.status === "completed" || t.status === "failed" || t.status === "cancelled") {
          resolve(t);
        } else {
          if (taskId === currentTaskId) updateProgressUI(t);
          setTimeout(check, 800);
        }
      } catch (e) {
        if (++errors >= 5) resolve({ status: "failed", error: String(e) });
        else setTimeout(check, 800);
      }
    };
    check();
  });
}

async function submitTask() {
  if (clipMode() === "batch") {
    if (batchQueue.length === 0) {
      toast("批量裁剪需要先把文件加入队列", "warn");
      return;
    }
    await runBatch();
  } else {
    if (!$("inputPath").value) {
      toast(batchQueue.length > 0
        ? "当前是单文件模式：请先选择文件；要处理队列请切换到「批量裁剪」"
        : "请先选择输入文件", "warn");
      return;
    }
    await runSingle();
  }
}

async function runSingle() {
  const platform = $("platform").value;
  const inputPath = $("inputPath").value;
  const outputPath = $("outputPath").value;

  let payload;

  if (platform === "smart") {
    // 智能模式：优先用缓存的源信息，没有就现取一次
    let srcInfo = currentSrcInfo;
    if (!srcInfo) {
      if (!inputPath) { toast("请先选择输入文件", "warn"); return; }
      const r = await fetch("/api/info?path=" + encodeURIComponent(inputPath));
      srcInfo = await r.json();
    }
    payload = buildPayload(inputPath, outputPath, { params: resolveSmartParams(srcInfo) });
  } else {
    payload = buildPayload(inputPath, outputPath);
  }

  if (!payload.input) { toast("请先选择输入文件", "warn"); return; }
  if (!payload.output) { toast("请填写输出文件路径", "warn"); return; }

  const btn = $("submitBtn");
  const stopBtn = $("stopBtn");
  btn.disabled = true;
  btn.textContent = "提交中…";
  $("openDirBtn").style.display = "none";

  try {
    const d = await postTranscode(payload);
    if (d.error) {
      toast(d.error, "error");
      btn.disabled = false;
      btn.textContent = "开始转码";
      return;
    }
    currentTaskId = d.task_id;
    saveActiveTask(d.task_id, payload.input, payload.output);
    $("progressWrap").style.display = "block";
    $("logBox").style.display = "block";
    $("logBox").textContent = "$ " + (d.cmd || "");
    if (d.audio_note) {
      $("logBox").textContent += "\n\n[提示] " + d.audio_note;
    }
    stopBtn.style.display = "inline-block";
    stopBtn.disabled = false;
    btn.textContent = "运行中…";

    // 轮询到任务结束，统一切换到完成 / 中止 / 失败收尾
    const t = await waitForTask(d.task_id);
    currentTaskId = null;
    finishSingleTask(t);
  } catch (e) {
    toast("提交失败: " + e, "error");
    btn.disabled = false;
    btn.textContent = "开始转码";
  }
}

// 单任务结束后的界面收尾（restored=true 表示刷新后还原，不再弹提示/重复刷新列表）
function finishSingleTask(t, restored) {
  const btn = $("submitBtn");
  const stopBtn = $("stopBtn");
  updateProgressUI(t);
  btn.disabled = false;
  stopBtn.disabled = false;
  stopBtn.style.display = "none";
  stopBtn.textContent = "中止";

  if (t.status === "completed") {
    btn.textContent = "✅ 完成，再来一次";
    if (!restored) refreshView();  // 刷新当前视图（含探查结果），显示新生成的文件
    $("openDirBtn").style.display = "inline-block";
  } else if (t.status === "cancelled") {
    btn.textContent = "🚫 已中止，重新开始";
  } else {
    btn.textContent = "❌ 失败，重试";
    if (!restored) toast("任务失败：" + (t.error || "未知错误"), "error");
  }
  if (t.error) {
    $("logBox").textContent += "\n\n[提示] " + t.error;
  }
}

async function runBatch(opts) {
  opts = opts || {};
  const resume = !!opts.resume;   // resume=true：刷新后接管未完成的队列，跳过已结束的项
  if (isBatchRunning) return;
  isBatchRunning = true;
  batchCancelled = false;
  batchTotal = batchQueue.length;

  if (resume) {
    batchDone = batchQueue.filter(it => isTerminalStatus(it.status)).length;
  } else {
    batchDone = 0;
    batchQueue.forEach(item => {
      item.status = "pending";
      item.taskId = null;
      item.error = "";
      item.outName = "";
    });
  }
  renderQueue();
  saveBatchState();
  clearActiveTask();   // 批量任务接管进度区，单文件恢复状态不再需要

  const btn = $("submitBtn");
  const stopBtn = $("stopBtn");
  btn.disabled = true;
  stopBtn.style.display = "inline-block";
  stopBtn.disabled = false;
  $("progressWrap").style.display = "block";
  $("logBox").style.display = "block";
  $("openDirBtn").style.display = "none";

  const platform = $("platform").value;
  let successCount = batchQueue.filter(it => it.status === "completed").length;
  let failedCount = batchQueue.filter(it => it.status === "failed").length;

  // 单个文件失败收尾：计数 + 更新队列状态
  const markFailed = (i, msg) => {
    failedCount++;
    batchDone++;
    updateQueueItem(i, { status: "failed", error: msg });
  };

  for (let i = 0; i < batchQueue.length; i++) {
    if (batchCancelled) {
      // 剩余的排队项直接标记中止，最后统一刷新一次
      let marked = false;
      for (let j = i; j < batchQueue.length; j++) {
        if (batchQueue[j].status === "pending") {
          batchQueue[j].status = "cancelled";
          marked = true;
        }
      }
      if (marked) { saveBatchState(); renderQueue(); }
      break;
    }

    const item = batchQueue[i];
    if (resume && isTerminalStatus(item.status)) continue;   // 已结束的项直接跳过

    const outPath = computeOutputPath(item.path, "batch");
    let taskId = null;

    if (resume && item.status === "running") {
      // 刷新前正在处理：能找回服务端任务就接管，找不回则退回待处理重新提交
      if (item.taskId) {
        taskId = item.taskId;
      } else {
        const found = await findTaskByOutput(outPath);
        if (found && (found.status === "pending" || found.status === "running")) {
          taskId = found.id;
          updateQueueItem(i, { taskId: taskId });
        }
      }
      if (taskId) {
        btn.textContent = `批量 ${i + 1}/${batchQueue.length}`;
        $("logBox").textContent =
          `[${i + 1}/${batchQueue.length}] ${item.name}\n（已接管刷新前进行中的任务，等待完成）`;
      } else {
        updateQueueItem(i, { status: "pending", error: "" });
      }
    }

    if (!taskId) {
      // 正常提交（智能模式每个文件单独分析）
      updateQueueItem(i, { status: "running", outName: splitPath(outPath).filename });
      btn.textContent = `批量 ${i + 1}/${batchQueue.length}`;

      let payload;

      if (platform === "smart") {
        try {
          $("logBox").textContent =
            `[${i + 1}/${batchQueue.length}] ${item.name}\n⏳ 正在分析源文件…`;
          const ir = await fetch("/api/info?path=" + encodeURIComponent(item.path));
          const srcInfo = await ir.json();
          if (srcInfo.error) {
            markFailed(i, "读取源信息失败");
            continue;
          }
          const params = resolveSmartParams(srcInfo);
          payload = buildPayload(item.path, outPath, { trim: "batch", params });
          $("logBox").textContent =
            `[${i + 1}/${batchQueue.length}] ${item.name}\n` +
            `🧠 智能选择：${params.platform} / ${params.format} / ` +
            `q=${params.quality_value}${params.bitrate ? " / " + params.bitrate : ""} / ${params.preset}`;
        } catch (e) {
          markFailed(i, "分析失败: " + e);
          continue;
        }
      } else {
        payload = buildPayload(item.path, outPath, { trim: "batch" });
      }

      try {
        const d = await postTranscode(payload);
        if (d.error) {
          markFailed(i, d.error);
          continue;
        }
        taskId = d.task_id;
        currentTaskId = taskId;
        updateQueueItem(i, { taskId: taskId });

        if (platform !== "smart") {
          $("logBox").textContent =
            `[${i + 1}/${batchQueue.length}] ${item.name}\n$ ${d.cmd || ""}` +
            (d.audio_note ? `\n\n[提示] ${d.audio_note}` : "");
        } else if (d.audio_note) {
          $("logBox").textContent += `\n\n[提示] ${d.audio_note}`;
        }
      } catch (e) {
        markFailed(i, String(e));
        continue;
      }
    } else {
      currentTaskId = taskId;
    }

    try {
      const result = await waitForTask(taskId);
      batchDone++;
      if (result.status === "completed") {
        successCount++;
        updateQueueItem(i, { status: "completed" });
      } else if (result.status === "cancelled") {
        updateQueueItem(i, { status: "cancelled" });
      } else {
        failedCount++;
        updateQueueItem(i, { status: "failed", error: result.error || "" });
      }
    } catch (e) {
      markFailed(i, String(e));
    }
  }

  currentTaskId = null;
  isBatchRunning = false;
  btn.disabled = false;
  stopBtn.style.display = "none";
  btn.textContent = `批量完成（成功 ${successCount} / 失败 ${failedCount}）`;
  setTimeout(() => { btn.textContent = "开始转码"; }, 3000);
  renderQueue();
  saveBatchState();

  // 刷新文件列表（显示新生成的文件），并提供打开输出目录入口
  refreshView();
  if (successCount > 0) {
    $("openDirBtn").style.display = "inline-block";
  }
  toast(`批量完成：成功 ${successCount} / 失败 ${failedCount}`,
        failedCount > 0 ? "warn" : "ok");
}

async function stopTask() {
  if (!currentTaskId && !isBatchRunning) return;
  const stopBtn = $("stopBtn");
  const msg = isBatchRunning ? "确定要中止批量任务吗？" : "确定要中止当前任务吗？";
  if (!confirm(msg + "已生成的部分文件会被保留。")) return;

  if (isBatchRunning) {
    batchCancelled = true;
    saveBatchState();
  }

  stopBtn.disabled = true;
  stopBtn.textContent = "中止中…";

  if (currentTaskId) {
    try {
      await fetch("/api/task/" + currentTaskId + "/stop", { method: "POST" });
    } catch (e) {}
  }
  if (!isBatchRunning) {
    stopBtn.style.display = "none";
    stopBtn.textContent = "中止";
  }
}

// 打开当前输出文件所在目录（在左侧文件浏览器中定位）
function openOutputDir() {
  const out = $("outputPath").value.trim();
  if (!out) { toast("还没有输出路径", "warn"); return; }
  const { dir } = splitPath(out);
  if (dir) loadPath(dir);
}

// ==================== 初始化 ====================
loadSettings();
loadPath(currentDir);
updateClipUI();
renderFavorites();
renderOutputDirs();
restoreSession();   // 恢复上次未完成 / 刚结束的任务与批量队列

// 文件夹选择弹窗：点击遮罩 / 按 Esc 关闭
$("folderModal").addEventListener("click", (e) => {
  if (e.target.id === "folderModal") closeFolderPicker();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("folderModal").style.display !== "none") {
    closeFolderPicker();
  }
});