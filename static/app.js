// ==================== 全局状态 ====================
let currentDir = window.MEDIA_DIR || "/";
let videoDuration = 0;
let availableEncoders = {};
let vaapiDeviceOk = false;
let currentTaskId = null;
let pollTimer = null;
let outputManuallyEdited = false;
let lastRecommendation = null;
let currentSrcInfo = null;

let batchQueue = [];
let isBatchRunning = false;
let batchCancelled = false;
let batchTotal = 0;
let batchDone = 0;

const FAV_KEY = "ffmpeg_web_favorites";
const OUT_DIR_KEY = "ffmpeg_web_output_dirs";

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
  const el = document.getElementById("ffmpegStatus");
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
  onPlatformChange();
});

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
  const sel = document.getElementById("platform");
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
function onPlatformChange() {
  const platform = document.getElementById("platform").value;
  const formatSelect = document.getElementById("format");
  const qualityLabel = document.getElementById("qualityLabel");
  const qualityHint = document.getElementById("qualityHint");
  const presetSelect = document.getElementById("preset");
  const presetLabel = document.getElementById("presetLabel");
  const qualityValue = document.getElementById("qualityValue");
  const bitrateInput = document.getElementById("bitrate");
  const scaleInput = document.getElementById("scale");
  const platformHint = document.getElementById("platformHint");
  const qualityModeSel = document.getElementById("qualityMode");

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
    document.getElementById("bitrateGroup").style.display = "none";
    platformHint.textContent = "只做剪切/封装，不重新编码，速度极快";
    if (!outputManuallyEdited) updateOutputPath();
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
    document.getElementById("bitrateGroup").style.display = "none";

    const gpu = pickGpuPlatform();
    if (gpu) {
      platformHint.textContent = `按每个源自动分析，优先使用 ${GPU_NAMES[gpu]}，回退 CPU。适合批量`;
    } else {
      platformHint.textContent = "按每个源自动分析，使用 CPU 编码。适合批量";
    }
    if (!outputManuallyEdited) updateOutputPath();
    return;
  }

  // === 手动平台 ===
  formatSelect.querySelector('option[value="av1"]').disabled = false;

  if (platform === "software") {
    qualityLabel.textContent = "CRF 质量";
    qualityHint.textContent = "越小越好，18-28 常用";
    presetLabel.textContent = "Preset";
    presetSelect.value = "medium";
    enablePresets(["ultrafast", "fast", "medium", "slow", "veryslow"]);
    formatSelect.querySelector('option[value="av1"]').disabled = !availableEncoders["libsvtav1"];
    platformHint.textContent = "CPU 编码，速度慢但画质和压缩率最好";
  } else if (platform === "nvenc") {
    qualityLabel.textContent = "CQ 质量";
    qualityHint.textContent = "NVENC 恒定质量，推荐 20-28";
    presetLabel.textContent = "Preset (p1-p7)";
    presetSelect.value = "p4";
    enablePresets(["p1", "p2", "p3", "p4", "p5", "p6", "p7"]);
    formatSelect.querySelector('option[value="av1"]').disabled = !availableEncoders["av1_nvenc"];
    platformHint.textContent = "NVIDIA 显卡硬件编码，速度极快";
  } else if (platform === "qsv") {
    qualityLabel.textContent = "ICQ 质量";
    qualityHint.textContent = "QSV 智能质量，推荐 22-28";
    presetLabel.textContent = "Preset";
    presetSelect.value = "medium";
    enablePresets(["veryfast", "faster", "fast", "medium", "slow", "slower"]);
    formatSelect.querySelector('option[value="av1"]').disabled = !availableEncoders["av1_qsv"];
    platformHint.textContent = "Intel 核显硬件编码，低功耗";
  } else if (platform === "amf") {
    qualityLabel.textContent = "QP 质量";
    qualityHint.textContent = "AMF 恒定 QP，推荐 22-26";
    presetLabel.textContent = "Preset";
    presetSelect.value = "balanced";
    enablePresets(["speed", "balanced", "quality"]);
    formatSelect.querySelector('option[value="av1"]').disabled = true;
    if (formatSelect.value === "av1") formatSelect.value = "h264";
    platformHint.textContent = "AMD 显卡硬件编码";
  } else if (platform === "vaapi") {
    qualityLabel.textContent = "QP 质量";
    qualityHint.textContent = "VAAPI 恒定 QP，推荐 22-26";
    presetLabel.textContent = "Preset";
    presetSelect.value = "medium";
    enablePresets(["ultrafast", "fast", "medium", "slow", "veryslow"]);
    formatSelect.querySelector('option[value="av1"]').disabled = !availableEncoders["av1_vaapi"];
    platformHint.textContent = "Linux 通用硬件加速接口";
  }

  onQualityModeChange();
  if (!outputManuallyEdited) updateOutputPath();
}

function enablePresets(list) {
  const presetSelect = document.getElementById("preset");
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
  const platform = document.getElementById("platform").value;
  const mode = document.getElementById("qualityMode").value;
  const bg = document.getElementById("bitrateGroup");
  const qv = document.getElementById("qualityValue");
  const br = document.getElementById("bitrate");
  const presetSelect = document.getElementById("preset");
  const formatSelect = document.getElementById("format");
  const scaleInput = document.getElementById("scale");

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
  const box = document.getElementById("recommendBox");
  const title = document.getElementById("recommendTitle");
  const hint = document.getElementById("recommendHint");
  const btnCpu = document.getElementById("applyCpuBtn");
  const btnGpu = document.getElementById("applyGpuBtn");

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

  document.getElementById("platform").value = platform;
  document.getElementById("format").value = "h265";
  onPlatformChange();

  if (rec.mode === "bitrate") {
    document.getElementById("qualityMode").value = "bitrate";
    document.getElementById("bitrate").value = rec.bitrate;
    document.getElementById("qualityValue").value = qualityValue;
  } else {
    document.getElementById("qualityMode").value = "quality";
    document.getElementById("qualityValue").value = qualityValue;
    document.getElementById("bitrate").value = "";
  }
  onQualityModeChange();
  document.getElementById("preset").value = preset;

  if (!outputManuallyEdited) updateOutputPath();

  const btn = (mode === "gpu")
    ? document.getElementById("applyGpuBtn")
    : document.getElementById("applyCpuBtn");
  if (btn) {
    const orig = btn.textContent;
    btn.textContent = "✓ 已应用";
    setTimeout(() => { btn.textContent = orig; }, 1200);
  }
}

// ==================== 输出路径智能生成 ====================
function isClipping() {
  const s = document.getElementById("startTime").value.trim();
  const e = document.getElementById("endTime").value.trim();
  if ((s && s !== "00:00:00") || e) return true;
  const h = document.getElementById("headTrim").value.trim();
  const t = document.getElementById("tailTrim").value.trim();
  if ((h && h !== "00:00:00") || (t && t !== "00:00:00")) return true;
  return false;
}

function isTranscoding() {
  return document.getElementById("platform").value !== "copy";
}

function currentCodecTag() {
  const p = document.getElementById("platform").value;
  if (p === "smart") return "h265";  // 智能模式默认输出 H.265
  return (document.getElementById("format").value || "h264").toLowerCase();
}

function calcSuffix(sameDir) {
  const clip = isClipping();
  const trans = isTranscoding();
  const codec = currentCodecTag();
  if (clip && trans) return "_cut_" + codec;
  if (clip) return "_cut";
  if (trans) return sameDir ? "_" + codec : "";
  return "";
}

function pickOutputExt(inputExt) {
  if (!isTranscoding()) return inputExt;
  const lower = (inputExt || "").toLowerCase();
  if (lower === ".mkv") return ".mkv";
  if (lower === ".webm") return ".webm";
  return ".mp4";
}

function computeOutputPath(inputPath) {
  if (!inputPath) return "";
  const { dir, base, ext } = splitPath(inputPath);
  const suffix = calcSuffix(true);
  const outExt = pickOutputExt(ext);
  return joinPath(dir, base + suffix + outExt);
}

function updateOutputPath() {
  const inputPath = document.getElementById("inputPath").value;
  if (!inputPath) return;
  document.getElementById("outputPath").value = computeOutputPath(inputPath);
}

function onTimeChanged() {
  updateClipUI();
  if (!outputManuallyEdited) updateOutputPath();
}

document.getElementById("outputPath").addEventListener("input", () => {
  outputManuallyEdited = true;
});

// 批量裁剪输入格式化
function setupTrimInput(id) {
  const el = document.getElementById(id);
  el.addEventListener("blur", function () {
    const v = this.value.trim();
    this.value = v === "" ? "00:00:00" : formatTime(parseTime(v));
    if (!outputManuallyEdited) updateOutputPath();
  });
  el.addEventListener("keydown", e => {
    if (e.key === "Enter") e.target.blur();
  });
  el.addEventListener("input", () => {
    if (!outputManuallyEdited) updateOutputPath();
  });
}
setupTrimInput("headTrim");
setupTrimInput("tailTrim");

// ==================== 批量队列 ====================
const STATUS_MAP = {
  pending:   { icon: "⏳", text: "待处理" },
  running:   { icon: "▶",  text: "处理中" },
  completed: { icon: "✓",  text: "完成" },
  failed:    { icon: "✗",  text: "失败" },
  cancelled: { icon: "⏸",  text: "已中止" },
};

function renderQueue() {
  const ul = document.getElementById("queueList");
  const countEl = document.getElementById("queueCount");
  const hintEl = document.getElementById("queueHint");

  countEl.textContent = batchQueue.length;
  ul.innerHTML = "";

  if (batchQueue.length === 0) {
    hintEl.style.display = "block";
    document.getElementById("queueFooter").style.display = "none";
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
        <span class="q-status-text">${st.text}${item.error ? " · " + item.error.slice(0, 40) : ""}</span>
      </div>
      <button class="q-remove" title="移除">×</button>
    `;
    li.querySelector(".q-name").textContent = item.name;

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
    document.getElementById("queueFooter").style.display = "block";
    const pct = batchTotal > 0 ? (batchDone / batchTotal) * 100 : 0;
    document.getElementById("queueProgressBar").style.width = pct + "%";
    document.getElementById("queueProgressText").textContent =
      `${batchDone} / ${batchTotal}（${pct.toFixed(0)}%）`;
  } else if (batchTotal > 0 && batchDone === batchTotal) {
    document.getElementById("queueFooter").style.display = "block";
    document.getElementById("queueProgressBar").style.width = "100%";
    document.getElementById("queueProgressText").textContent =
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
  renderQueue();
}

function removeFromQueue(idx) {
  if (isBatchRunning) {
    alert("批量处理中，无法移除队列项");
    return;
  }
  batchQueue.splice(idx, 1);
  renderQueue();
  // 同步左侧按钮状态
  refreshFileListAddButtons();
}

function clearQueue() {
  if (isBatchRunning) {
    alert("批量处理中，无法清空队列");
    return;
  }
  if (batchQueue.length === 0) return;
  if (!confirm(`确定清空队列中的 ${batchQueue.length} 个文件吗？`)) return;
  batchQueue = [];
  batchTotal = 0;
  batchDone = 0;
  renderQueue();
  refreshFileListAddButtons();
}

function clearCompleted() {
  if (isBatchRunning) {
    alert("批量处理中，无法清空队列");
    return;
  }
  const before = batchQueue.length;
  batchQueue = batchQueue.filter(it => it.status === "pending" || it.status === "running");
  if (batchQueue.length === before) {
    alert("没有已完成的项目");
    return;
  }
  renderQueue();
  refreshFileListAddButtons();
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
  renderQueue();
}

// 单个文件的 payload（批量模式）
function buildPayloadForFile(inputPath, outputPath) {
  const mode = document.getElementById("qualityMode").value;
  const platform = document.getElementById("platform").value;
  const headTrim = document.getElementById("headTrim").value.trim();
  const tailTrim = document.getElementById("tailTrim").value.trim();

  return {
    input: inputPath,
    output: outputPath,
    start_time: "",
    end_time: "",
    head_trim: headTrim,
    tail_trim: tailTrim,
    platform: platform,
    format: document.getElementById("format").value,
    quality_value: document.getElementById("qualityValue").value,
    bitrate: (platform !== "copy" && platform !== "smart" && mode === "bitrate")
      ? document.getElementById("bitrate").value : "",
    preset: document.getElementById("preset").value,
    scale: document.getElementById("scale").value,
    acodec: document.getElementById("acodec").value,
    abitrate: document.getElementById("abitrate").value,
    extra: document.getElementById("extra").value,
  };
}

// 用指定参数构建 payload（用于智能模式）
function buildPayloadWithParams(inputPath, outputPath, params) {
  const headTrim = document.getElementById("headTrim").value.trim();
  const tailTrim = document.getElementById("tailTrim").value.trim();

  return {
    input: inputPath,
    output: outputPath,
    start_time: "",
    end_time: "",
    head_trim: headTrim,
    tail_trim: tailTrim,
    platform: params.platform,
    format: params.format,
    quality_value: params.quality_value,
    bitrate: params.bitrate,
    preset: params.preset,
    scale: document.getElementById("scale").value,
    acodec: document.getElementById("acodec").value,
    abitrate: document.getElementById("abitrate").value,
    extra: document.getElementById("extra").value,
  };
}

// ==================== 收藏夹 ====================
function getFavorites() {
  try { return JSON.parse(localStorage.getItem(FAV_KEY) || "[]"); }
  catch (e) { return []; }
}
function saveFavorites(list) { localStorage.setItem(FAV_KEY, JSON.stringify(list)); }
function renderFavorites() {
  const list = getFavorites();
  const el = document.getElementById("favList");
  el.innerHTML = "";
  if (list.length === 0) {
    el.innerHTML = '<span class="fav-empty">点右上 + 收藏当前目录</span>';
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
      loadPath(fav.path);
    };
    item.querySelector(".fav-remove").onclick = (e) => {
      e.stopPropagation();
      removeFavorite(idx);
    };
    el.appendChild(item);
  });
}
function addCurrentToFavorites() {
  if (!currentDir) return;
  const list = getFavorites();
  if (list.some(f => f.path === currentDir)) { alert("这个目录已经在收藏夹里了"); return; }
  const segments = currentDir.split(/[\/\\]/).filter(Boolean);
  const name = segments.length > 0 ? segments[segments.length - 1] : currentDir;
  list.push({ name, path: currentDir });
  saveFavorites(list);
  renderFavorites();
}
function removeFavorite(idx) {
  const list = getFavorites();
  list.splice(idx, 1);
  saveFavorites(list);
  renderFavorites();
}

// ==================== 输出目录收藏 ====================
function getOutputDirs() {
  try { return JSON.parse(localStorage.getItem(OUT_DIR_KEY) || "[]"); }
  catch (e) { return []; }
}
function saveOutputDirs(list) { localStorage.setItem(OUT_DIR_KEY, JSON.stringify(list)); }
function renderOutputDirs() {
  const list = getOutputDirs();
  const el = document.getElementById("outputDirList");
  el.innerHTML = "";
  if (list.length === 0) {
    el.innerHTML = '<span class="fav-empty">点右上 + 收藏当前输出目录</span>';
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
      applyOutputDir(fav.path);
    };
    item.querySelector(".fav-remove").onclick = (e) => {
      e.stopPropagation();
      removeOutputDir(idx);
    };
    el.appendChild(item);
  });
}
function addCurrentOutputDir() {
  const outPath = document.getElementById("outputPath").value.trim();
  if (!outPath) { alert("输出文件路径为空"); return; }
  const { dir } = splitPath(outPath);
  if (!dir) { alert("无法解析目录"); return; }
  const list = getOutputDirs();
  if (list.some(f => f.path === dir)) { alert("这个目录已经在输出目录列表里了"); return; }
  const segments = dir.split(/[\/\\]/).filter(Boolean);
  const name = segments.length > 0 ? segments[segments.length - 1] : dir;
  list.push({ name, path: dir });
  saveOutputDirs(list);
  renderOutputDirs();
}
function removeOutputDir(idx) {
  const list = getOutputDirs();
  list.splice(idx, 1);
  saveOutputDirs(list);
  renderOutputDirs();
}

function applyOutputDir(newDir) {
  const inputPath = document.getElementById("inputPath").value.trim();
  if (!inputPath) { alert("请先选择输入文件"); return; }

  const { dir: inputDir, base, ext } = splitPath(inputPath);
  const norm = (p) => p.replace(/[\/\\]+$/, "").replace(/\\/g, "/").toLowerCase();
  const sameDir = norm(newDir) === norm(inputDir);

  const suffix = calcSuffix(sameDir);
  const outExt = pickOutputExt(ext);
  const filename = base + suffix + outExt;
  document.getElementById("outputPath").value = joinPath(newDir, filename);
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
const player = document.getElementById("player");
const videoPlaceholder = document.getElementById("videoPlaceholder");
const videoArea = document.getElementById("videoArea");

player.addEventListener("loadedmetadata", () => {
  videoDuration = player.duration || 0;
  videoPlaceholder.style.display = "none";
  document.getElementById("endTime").placeholder = formatTime(videoDuration);
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
  document.getElementById("startTime").value = formatTime(player.currentTime || 0);
  onTimeChanged();
}
function setEndFromPlayer() {
  if (!player.src) return;
  document.getElementById("endTime").value = formatTime(player.currentTime || 0);
  onTimeChanged();
}

document.getElementById("startTime").addEventListener("blur", function () {
  const v = this.value.trim();
  this.value = v === "" ? "00:00:00" : formatTime(parseTime(v));
  onTimeChanged();
});
document.getElementById("endTime").addEventListener("blur", function () {
  const v = this.value.trim();
  this.value = v === "" ? "" : formatTime(parseTime(v));
  onTimeChanged();
});
["startTime", "endTime"].forEach(id => {
  document.getElementById(id).addEventListener("keydown", e => {
    if (e.key === "Enter") e.target.blur();
  });
  document.getElementById(id).addEventListener("input", onTimeChanged);
});

function updateClipUI() {
  const s = parseTime(document.getElementById("startTime").value);
  const eRaw = document.getElementById("endTime").value.trim();
  const e = eRaw ? parseTime(eRaw) : (videoDuration || 0);

  document.getElementById("clipStartTip").textContent = formatTime(s);
  document.getElementById("clipEndTip").textContent =
    eRaw ? formatTime(e) : (videoDuration ? formatTime(videoDuration) : "--:--:--");

  const bar = document.getElementById("clipBarActive");
  if (videoDuration > 0) {
    const leftPct = Math.min(100, Math.max(0, (s / videoDuration) * 100));
    const rightPct = Math.min(100, Math.max(0, 100 - (e / videoDuration) * 100));
    bar.style.left = leftPct + "%";
    bar.style.right = rightPct + "%";
  } else {
    bar.style.left = "0%";
    bar.style.right = "0%";
  }

  const hint = document.getElementById("durationHint");
  let dur = 0;
  if (e > s) dur = e - s;
  else if (videoDuration > 0) dur = videoDuration - s;
  hint.textContent = dur > 0 ? `📐 剪辑时长：${formatTime(dur)}` : "📐 剪辑时长：--";
}

// ==================== 文件浏览 ====================
async function loadPath(path) {
  try {
    const r = await fetch("/api/browse?path=" + encodeURIComponent(path));
    const d = await r.json();
    if (d.error) { alert(d.error); return; }
    currentDir = d.path;
    document.getElementById("pathInput").value = d.path;

    const ul = document.getElementById("filelist");
    ul.innerHTML = "";

    if (d.parent) {
      const li = document.createElement("li");
      li.className = "up";
      li.innerHTML = `<span class="icon">📁</span><span class="name">..</span>`;
      li.onclick = () => loadPath(d.parent);
      ul.appendChild(li);
    }

    if (d.items.length === 0) {
      ul.innerHTML += `<li class="empty">（空目录，无视频文件）</li>`;
    }

    d.items.forEach(item => {
      const li = document.createElement("li");
      li.dataset.path = item.path;
      const icon = item.is_dir ? "📁" : "🎞️";
      const size = item.size_human ? `<span class="size">${item.size_human}</span>` : "";

      if (item.is_dir) {
        li.innerHTML = `<span class="icon">${icon}</span><span class="name"></span>${size}`;
        li.querySelector(".name").textContent = item.name;
        li.onclick = () => loadPath(item.path);
      } else {
        const inQueue = batchQueue.some(q => q.path === item.path);
        li.innerHTML = `
          <span class="icon">${icon}</span>
          <span class="name"></span>
          ${size}
          <button class="add-btn${inQueue ? ' added' : ''}" title="${inQueue ? '已在队列' : '加入队列'}">${inQueue ? '✓' : '+'}</button>
        `;
        li.querySelector(".name").textContent = item.name;

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
      }
      ul.appendChild(li);
    });
  } catch (e) {
    alert("加载失败: " + e);
  }
}

async function previewFile(path) {
  document.getElementById("inputPath").value = path;
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
  const box = document.getElementById("infoBox");

  if (d.error) {
    box.style.display = "block";
    box.innerHTML = `<b style="color:#ff6b6b">读取视频信息失败：</b>
      <span style="color:#ccc">${d.error}</span>`;
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
  document.getElementById("startTime").value = "00:00:00";
  document.getElementById("endTime").value = "";
  videoDuration = 0;
  updateClipUI();
  outputManuallyEdited = false;
  updateOutputPath();
}

// ==================== 提交 / 中止 ====================
function waitForTask(taskId) {
  return new Promise((resolve) => {
    const check = async () => {
      try {
        const r = await fetch("/api/task/" + taskId);
        const t = await r.json();
        if (t.status === "completed" || t.status === "failed" || t.status === "cancelled") {
          resolve(t);
        } else {
          if (taskId === currentTaskId) {
            document.getElementById("progressFill").style.width = (t.progress || 0) + "%";
            document.getElementById("progressPct").textContent = (t.progress || 0).toFixed(1) + "%";
            const stateEl = document.getElementById("taskState");
            stateEl.textContent = t.status;
            stateEl.className = "tag-status s-" + t.status;
            if (t.log) {
              const box = document.getElementById("logBox");
              box.textContent = t.log;
              box.scrollTop = box.scrollHeight;
            }
          }
          setTimeout(check, 800);
        }
      } catch (e) {
        resolve({ status: "failed", error: String(e) });
      }
    };
    check();
  });
}

async function submitTask() {
  if (batchQueue.length > 0) {
    await runBatch();
  } else {
    await runSingle();
  }
}

async function runSingle() {
  const platform = document.getElementById("platform").value;
  const mode = document.getElementById("qualityMode").value;

  let payload;

  if (platform === "smart") {
    // 智能模式：用缓存的源信息
    let srcInfo = currentSrcInfo;
    if (!srcInfo) {
      const inputPath = document.getElementById("inputPath").value;
      if (!inputPath) { alert("请先选择输入文件"); return; }
      const r = await fetch("/api/info?path=" + encodeURIComponent(inputPath));
      srcInfo = await r.json();
    }
    const params = resolveSmartParams(srcInfo);
    payload = {
      input: document.getElementById("inputPath").value,
      output: document.getElementById("outputPath").value,
      start_time: document.getElementById("startTime").value,
      end_time: document.getElementById("endTime").value,
      head_trim: "",
      tail_trim: "",
      platform: params.platform,
      format: params.format,
      quality_value: params.quality_value,
      bitrate: params.bitrate,
      preset: params.preset,
      scale: document.getElementById("scale").value,
      acodec: document.getElementById("acodec").value,
      abitrate: document.getElementById("abitrate").value,
      extra: document.getElementById("extra").value,
    };
  } else {
    payload = {
      input: document.getElementById("inputPath").value,
      output: document.getElementById("outputPath").value,
      start_time: document.getElementById("startTime").value,
      end_time: document.getElementById("endTime").value,
      head_trim: "",
      tail_trim: "",
      platform: platform,
      format: document.getElementById("format").value,
      quality_value: document.getElementById("qualityValue").value,
      bitrate: (platform !== "copy" && mode === "bitrate") ? document.getElementById("bitrate").value : "",
      preset: document.getElementById("preset").value,
      scale: document.getElementById("scale").value,
      acodec: document.getElementById("acodec").value,
      abitrate: document.getElementById("abitrate").value,
      extra: document.getElementById("extra").value,
    };
  }

  if (!payload.input) { alert("请先选择输入文件"); return; }
  if (!payload.output) { alert("请填写输出文件路径"); return; }

  const btn = document.getElementById("submitBtn");
  const stopBtn = document.getElementById("stopBtn");
  btn.disabled = true;
  btn.textContent = "提交中…";

  try {
    const r = await fetch("/api/transcode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (d.error) {
      alert(d.error);
      btn.disabled = false;
      btn.textContent = "开始转码";
      return;
    }
    currentTaskId = d.task_id;
    document.getElementById("progressWrap").style.display = "block";
    document.getElementById("logBox").style.display = "block";
    document.getElementById("logBox").textContent = "$ " + (d.cmd || "");
    if (d.audio_note) {
      document.getElementById("logBox").textContent += "\n\n[提示] " + d.audio_note;
    }
    stopBtn.style.display = "inline-block";
    stopBtn.disabled = false;
    btn.textContent = "运行中…";
    startPolling();
  } catch (e) {
    alert("提交失败: " + e);
    btn.disabled = false;
    btn.textContent = "开始转码";
  }
}

async function runBatch() {
  if (isBatchRunning) return;
  isBatchRunning = true;
  batchCancelled = false;
  batchTotal = batchQueue.length;
  batchDone = 0;

  batchQueue.forEach(item => {
    item.status = "pending";
    item.taskId = null;
    item.error = "";
  });
  renderQueue();

  const btn = document.getElementById("submitBtn");
  const stopBtn = document.getElementById("stopBtn");
  btn.disabled = true;
  stopBtn.style.display = "inline-block";
  stopBtn.disabled = false;
  document.getElementById("progressWrap").style.display = "block";
  document.getElementById("logBox").style.display = "block";

  const platform = document.getElementById("platform").value;
  let successCount = 0;
  let failedCount = 0;

  for (let i = 0; i < batchQueue.length; i++) {
    if (batchCancelled) {
      for (let j = i; j < batchQueue.length; j++) {
        if (batchQueue[j].status === "pending") {
          updateQueueItem(j, { status: "cancelled" });
        }
      }
      break;
    }

    const item = batchQueue[i];
    updateQueueItem(i, { status: "running" });
    btn.textContent = `批量 ${i + 1}/${batchQueue.length}`;

    const outPath = computeOutputPath(item.path);
    let payload;

    if (platform === "smart") {
      // 智能模式：每个文件单独分析
      try {
        document.getElementById("logBox").textContent =
          `[${i + 1}/${batchQueue.length}] ${item.name}\n⏳ 正在分析源文件…`;
        const ir = await fetch("/api/info?path=" + encodeURIComponent(item.path));
        const srcInfo = await ir.json();
        if (srcInfo.error) {
          updateQueueItem(i, { status: "failed", error: "读取源信息失败" });
          failedCount++;
          batchDone++;
          renderQueue();
          continue;
        }
        const params = resolveSmartParams(srcInfo);
        payload = buildPayloadWithParams(item.path, outPath, params);
        document.getElementById("logBox").textContent =
          `[${i + 1}/${batchQueue.length}] ${item.name}\n` +
          `🧠 智能选择：${params.platform} / ${params.format} / ` +
          `q=${params.quality_value}${params.bitrate ? " / " + params.bitrate : ""} / ${params.preset}`;
      } catch (e) {
        updateQueueItem(i, { status: "failed", error: "分析失败: " + e });
        failedCount++;
        batchDone++;
        renderQueue();
        continue;
      }
    } else {
      payload = buildPayloadForFile(item.path, outPath);
    }

    try {
      const r = await fetch("/api/transcode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const d = await r.json();
      if (d.error) {
        updateQueueItem(i, { status: "failed", error: d.error });
        failedCount++;
        batchDone++;
        renderQueue();
        continue;
      }

      currentTaskId = d.task_id;
      updateQueueItem(i, { taskId: d.task_id });

      if (platform !== "smart") {
        document.getElementById("logBox").textContent =
          `[${i + 1}/${batchQueue.length}] ${item.name}\n$ ${d.cmd || ""}` +
          (d.audio_note ? `\n\n[提示] ${d.audio_note}` : "");
      } else if (d.audio_note) {
        document.getElementById("logBox").textContent += `\n\n[提示] ${d.audio_note}`;
      }

      const result = await waitForTask(d.task_id);
      if (result.status === "completed") {
        updateQueueItem(i, { status: "completed" });
        successCount++;
      } else if (result.status === "cancelled") {
        updateQueueItem(i, { status: "cancelled" });
      } else {
        updateQueueItem(i, { status: "failed", error: result.error || "" });
        failedCount++;
      }
      batchDone++;
      renderQueue();
    } catch (e) {
      updateQueueItem(i, { status: "failed", error: String(e) });
      failedCount++;
      batchDone++;
      renderQueue();
    }
  }

  currentTaskId = null;
  isBatchRunning = false;
  btn.disabled = false;
  stopBtn.style.display = "none";
  btn.textContent = `批量完成（成功 ${successCount} / 失败 ${failedCount}）`;
  setTimeout(() => { btn.textContent = "开始转码"; }, 3000);
  renderQueue();
}

async function stopTask() {
  if (!currentTaskId && !isBatchRunning) return;
  const stopBtn = document.getElementById("stopBtn");
  const msg = isBatchRunning ? "确定要中止批量任务吗？" : "确定要中止当前任务吗？";
  if (!confirm(msg + "已生成的部分文件会被保留。")) return;

  if (isBatchRunning) batchCancelled = true;

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

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (!currentTaskId) return;
    const r = await fetch("/api/task/" + currentTaskId);
    const t = await r.json();

    document.getElementById("progressFill").style.width = (t.progress || 0) + "%";
    document.getElementById("progressPct").textContent = (t.progress || 0).toFixed(1) + "%";

    const stateEl = document.getElementById("taskState");
    stateEl.textContent = t.status;
    stateEl.className = "tag-status s-" + t.status;

    if (t.log) {
      const box = document.getElementById("logBox");
      box.textContent = t.log;
      box.scrollTop = box.scrollHeight;
    }

    if (t.status === "completed" || t.status === "failed" || t.status === "cancelled") {
      clearInterval(pollTimer);
      const btn = document.getElementById("submitBtn");
      const stopBtn = document.getElementById("stopBtn");
      btn.disabled = false;
      stopBtn.style.display = "none";
      stopBtn.textContent = "中止";

      if (t.status === "completed") btn.textContent = "✅ 完成，再来一次";
      else if (t.status === "cancelled") btn.textContent = "🚫 已中止，重新开始";
      else btn.textContent = "❌ 失败，重试";

      if (t.error) {
        document.getElementById("logBox").textContent += "\n\n[提示] " + t.error;
      }
    }
  }, 800);
}

// ==================== 初始化 ====================
loadPath(currentDir);
updateClipUI();
renderFavorites();
renderOutputDirs();
renderQueue();