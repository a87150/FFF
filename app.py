import atexit
import os
import re
import json
import shutil
import platform
import subprocess
import threading
import time
import uuid
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"


def find_executable(name):
    candidates = []
    if IS_WINDOWS:
        for ext in (".exe", ".cmd", ".bat", ""):
            candidates.append(os.path.join(BASE_DIR, name + ext))
    else:
        candidates.append(os.path.join(BASE_DIR, name))
    for path in candidates:
        if os.path.isfile(path):
            return path
    found = shutil.which(name)
    return found or name


FFMPEG = find_executable("ffmpeg")
FFPROBE = find_executable("ffprobe")

if IS_WINDOWS:
    MEDIA_DIR = os.environ.get("MEDIA_DIR", os.path.expanduser("~"))
else:
    MEDIA_DIR = os.environ.get("MEDIA_DIR", "/")

VAAPI_DEVICE = os.environ.get("VAAPI_DEVICE", "/dev/dri/renderD128")

tasks = {}
tasks_lock = threading.Lock()

running_processes = {}
process_lock = threading.Lock()

# 同时运行的 FFmpeg 任务数上限（超出的任务先排队），可用环境变量调整
try:
    MAX_CONCURRENT_TASKS = max(1, int(os.environ.get("MAX_CONCURRENT_TASKS", "1")))
except ValueError:
    MAX_CONCURRENT_TASKS = 1
TASK_SEMAPHORE = threading.Semaphore(MAX_CONCURRENT_TASKS)

# tasks 字典最多保留的记录数（防止长时间运行后内存持续增长）
MAX_TASKS_KEPT = 200


def _prune_tasks_locked():
    """超出上限时清理最早的已结束任务（调用方需持有 tasks_lock）"""
    overflow = len(tasks) - MAX_TASKS_KEPT
    if overflow <= 0:
        return
    for tid in list(tasks.keys()):
        if overflow <= 0:
            break
        if tasks[tid].get("status") not in ("pending", "running"):
            del tasks[tid]
            overflow -= 1


def _kill_running_processes():
    """进程退出时清理残留的 FFmpeg 子进程"""
    with process_lock:
        procs = list(running_processes.values())
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass


atexit.register(_kill_running_processes)

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm",
              ".ts", ".m2ts", ".mpg", ".mpeg", ".vob", ".m4v", ".3gp", ".rmvb"}

# ===== 容器兼容性表 =====
# 扩展名 -> 允许的音频编码（MKV / AVI 等未列出的容器不做限制）
CONTAINER_AUDIO_CODECS = {
    "mp4": {"aac", "mp3", "ac3", "eac3", "alac", "opus"},
    "m4v": {"aac", "mp3", "ac3", "eac3", "alac", "opus"},
    "mov": {"aac", "mp3", "ac3", "eac3", "alac", "opus", "pcm_s16le"},
    "webm": {"opus", "vorbis"},
    "flv": {"aac", "mp3"},
    "ts": {"aac", "mp3", "ac3", "eac3"},
    "m2ts": {"aac", "mp3", "ac3", "eac3"},
}

# 扩展名 -> 允许的视频编码（用于 copy 模式的预检查）
CONTAINER_VIDEO_CODECS = {
    "mp4": {"h264", "hevc", "av1", "vp9", "mpeg4", "mpeg2video", "mjpeg"},
    "m4v": {"h264", "hevc", "av1", "vp9", "mpeg4", "mpeg2video", "mjpeg"},
    "mov": {"h264", "hevc", "av1", "vp9", "mpeg4", "mpeg2video", "mjpeg", "prores", "dnxhd"},
    "webm": {"vp8", "vp9", "av1"},
    "flv": {"h264", "flv1", "vp6", "vp6a", "vp6f", "h263"},
    "ts": {"h264", "hevc", "mpeg2video", "mpeg1video", "av1"},
    "m2ts": {"h264", "hevc", "mpeg2video", "mpeg1video", "av1"},
}

# 音频不兼容时自动切换的默认编码：扩展名 -> (编码器, 显示名)
CONTAINER_FALLBACK_ACODEC = {
    "mp4": ("aac", "AAC"),
    "m4v": ("aac", "AAC"),
    "mov": ("aac", "AAC"),
    "webm": ("libopus", "Opus"),
    "flv": ("aac", "AAC"),
    "ts": ("aac", "AAC"),
    "m2ts": ("aac", "AAC"),
}

# 这些容器支持 -movflags +faststart
FASTSTART_EXTS = {"mp4", "m4v", "mov"}

# 参数校验
BITRATE_RE = re.compile(r"^\d+(\.\d+)?[kKmM]?$")
SCALE_RE = re.compile(r"^-?\d+:-?\d+$")

# preset 白名单（前端控件之外的输入一律回退默认值）
VALID_PRESETS = {
    "software": {"ultrafast", "superfast", "veryfast", "faster", "fast",
                 "medium", "slow", "slower", "veryslow"},
    "nvenc": {"p1", "p2", "p3", "p4", "p5", "p6", "p7"},
    "qsv": {"veryfast", "faster", "fast", "medium", "slow", "slower"},
    "amf": {"speed", "balanced", "quality"},
}
DEFAULT_PRESETS = {"software": "medium", "nvenc": "p4", "qsv": "medium", "amf": "balanced"}


def output_ext(path):
    """取输出文件扩展名（小写、不含点），没有扩展名返回空串"""
    name = os.path.basename(path or "")
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def same_file(a, b):
    """比较两个路径是否指向同一文件（兼容 Windows 大小写 / 短路径 / 软链）"""
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except Exception:
        return os.path.abspath(a) == os.path.abspath(b)


def parse_time(t):
    if not t:
        return 0.0
    try:
        parts = str(t).strip().split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(t)
    except Exception:
        return 0.0


def run_subprocess(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        stdout = r.stdout.decode("utf-8", errors="replace").lstrip("\ufeff")
        stderr = r.stderr.decode("utf-8", errors="replace").lstrip("\ufeff")
        return r.returncode, stdout, stderr
    except FileNotFoundError as e:
        return -1, "", f"找不到可执行文件: {e}"
    except subprocess.TimeoutExpired:
        return -2, "", "执行超时"
    except Exception as e:
        return -3, "", f"执行出错: {e}"


def get_video_info(filepath):
    if not filepath or not os.path.exists(filepath):
        return {"error": f"文件不存在: {filepath}"}
    cmd = [FFPROBE, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", filepath]
    code, stdout, stderr = run_subprocess(cmd, timeout=30)
    if code != 0:
        return {"error": f"ffprobe 返回码 {code}: {stderr[:300] or '无 stderr 输出'}"}
    stdout = stdout.strip()
    if not stdout:
        return {"error": "ffprobe 输出为空（文件可能损坏或不是有效视频）"}
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        return {"error": f"JSON 解析失败: {e}；前 200 字符: {stdout[:200]}"}
    if not data.get("format") and not data.get("streams"):
        return {"error": f"ffprobe 返回了空数据；前 200 字符: {stdout[:200]}"}
    return data


def human_size(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def get_source_audio_codec(src_info):
    """从 ffprobe 结果里提取源音频编码名"""
    try:
        for s in src_info.get("streams", []):
            if s.get("codec_type") == "audio":
                return (s.get("codec_name") or "").lower()
    except Exception:
        pass
    return ""


def get_source_video_codec(src_info):
    """从 ffprobe 结果里提取源视频编码名"""
    try:
        for s in src_info.get("streams", []):
            if s.get("codec_type") == "video":
                return (s.get("codec_name") or "").lower()
    except Exception:
        pass
    return ""


def ensure_audio_compatible(out_ext, acodec, abitrate, src_info):
    """
    检查音频编码是否和目标容器兼容。
    如果用户选了 copy 但源编码不兼容，自动切换成对应容器的默认编码。
    返回 (acodec, abitrate, auto_switched, note)
    """
    if acodec != "copy":
        return acodec, abitrate, False, ""

    src_acodec = get_source_audio_codec(src_info)
    if not src_acodec:
        # 没有音频轨，无所谓
        return acodec, abitrate, False, ""

    allowed = CONTAINER_AUDIO_CODECS.get(out_ext)
    if not allowed or src_acodec in allowed:
        # 未收录的容器（mkv/avi 等）通常都兼容
        return acodec, abitrate, False, ""

    fallback = CONTAINER_FALLBACK_ACODEC.get(out_ext)
    if not fallback:
        return acodec, abitrate, False, ""

    new_codec, label = fallback
    default_br = "128k" if new_codec == "libopus" else "192k"
    return new_codec, (abitrate or default_br), True, \
           f"源音频 {src_acodec} 不能封装进 .{out_ext}，已自动改为 {label} 重编码"


def check_copy_video_compat(out_ext, src_info):
    """
    copy 模式：检查源视频编码能否封装进目标容器。
    返回错误信息字符串；兼容时返回 None。
    """
    allowed = CONTAINER_VIDEO_CODECS.get(out_ext)
    if not allowed:
        return None
    src_vcodec = get_source_video_codec(src_info)
    if not src_vcodec or src_vcodec in allowed:
        return None
    return (f"源视频编码 {src_vcodec.upper()} 无法封装进 .{out_ext} 容器，"
            f"请改用转码，或把输出扩展名改成兼容的容器（如 .mkv）")


def build_encoder(platform_name, fmt, quality_value, bitrate, preset, scale):
    pre, vf, enc_args = [], [], []
    use_bitrate = bool(bitrate)

    if platform_name == "vaapi" and not IS_LINUX:
        return None, [], [], []

    if platform_name == "software":
        enc = {"h264": "libx264", "h265": "libx265", "av1": "libsvtav1"}.get(fmt)
        if not enc:
            return None, [], [], []
        enc_args += ["-b:v", bitrate] if use_bitrate else ["-crf", str(quality_value)]
        if enc in ("libx264", "libx265"):
            enc_args += ["-preset", preset]
        if scale:
            vf.append(f"scale={scale}")

    elif platform_name == "nvenc":
        enc = {"h264": "h264_nvenc", "h265": "hevc_nvenc", "av1": "av1_nvenc"}.get(fmt)
        if not enc:
            return None, [], [], []
        if use_bitrate:
            enc_args += ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate]
        else:
            enc_args += ["-cq", str(quality_value)]
        enc_args += ["-preset", preset]
        if scale:
            vf.append(f"scale_cuda={scale}")

    elif platform_name == "qsv":
        enc = {"h264": "h264_qsv", "h265": "hevc_qsv", "av1": "av1_qsv"}.get(fmt)
        if not enc:
            return None, [], [], []
        if use_bitrate:
            enc_args += ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate]
        else:
            enc_args += ["-global_quality", str(quality_value)]
        enc_args += ["-preset", preset]
        if scale:
            vf.append(f"scale_qsv={scale}")

    elif platform_name == "amf":
        enc = {"h264": "h264_amf", "h265": "hevc_amf"}.get(fmt)
        if not enc:
            return None, [], [], []
        if use_bitrate:
            enc_args += ["-b:v", bitrate]
        else:
            enc_args += ["-rc", "cqp", "-qp_i", str(quality_value), "-qp_p", str(quality_value)]
        enc_args += ["-quality", preset]
        if scale:
            vf.append(f"scale={scale}")

    elif platform_name == "vaapi":
        enc = {"h264": "h264_vaapi", "h265": "hevc_vaapi", "av1": "av1_vaapi"}.get(fmt)
        if not enc:
            return None, [], [], []
        pre += ["-vaapi_device", VAAPI_DEVICE]
        # VAAPI 需要先转为 nv12 并上传到显卡，缩放要在上传之后
        vf.append("format=nv12")
        vf.append("hwupload")
        if scale:
            vf.append(f"scale_vaapi={scale}")
        enc_args += ["-b:v", bitrate] if use_bitrate else ["-qp", str(quality_value)]

    else:
        return None, [], [], []

    return enc, pre, vf, enc_args


def _apply_progress(task_id, current, duration, state):
    """根据当前输出时间更新任务进度 / 速度 / 剩余时间估计"""
    progress = None
    if duration and duration > 0 and current is not None:
        progress = min(99.9, (current / duration) * 100)

    eta = None
    speed_num = state.get("speed_num") or 0.0
    if duration and duration > 0 and current is not None and speed_num > 0:
        eta = max(0.0, (duration - current) / speed_num)

    with tasks_lock:
        task = tasks.get(task_id)
        if task is None:
            return
        if progress is not None:
            task["progress"] = round(progress, 2)
        if state.get("speed"):
            task["speed"] = state["speed"]
        if eta is not None:
            task["eta"] = round(eta, 1)


def _drain_stderr(process, task_id, log_lines, state, duration):
    """后台线程：持续读取 FFmpeg stderr 作为日志（老版本 FFmpeg 的回退进度解析）"""
    time_pattern = re.compile(rb"time=(\d+):(\d+):(\d+\.\d+)")
    for raw in process.stderr:
        line = raw.decode("utf-8", errors="replace").rstrip()
        log_lines.append(line)
        if len(log_lines) > 300:
            del log_lines[:-300]

        # 没有 -progress 输出时（极老版本 FFmpeg），回退解析 stderr 的 time=
        if not state.get("seen") and duration and duration > 0:
            m = time_pattern.search(raw)
            if m:
                h, mi, s = m.groups()
                current = int(h) * 3600 + int(mi) * 60 + float(s)
                state["current"] = current
                _apply_progress(task_id, current, duration, state)

        with tasks_lock:
            task = tasks.get(task_id)
            if task is not None:
                task["log"] = "\n".join(log_lines[-25:])


def run_ffmpeg_task(task_id, cmd, duration=None):
    # 并发上限：超出的任务在这里排队等待（任务状态保持 pending）
    with TASK_SEMAPHORE:
        with tasks_lock:
            task = tasks.get(task_id)
            if task is None:
                return
            if task.get("cancelled") or task["status"] == "cancelled":
                task["status"] = "cancelled"
                task["error"] = task.get("error") or "用户已中止任务"
                task.setdefault("end_time", datetime.now().isoformat())
                return
            task["status"] = "running"
            task["start_time"] = datetime.now().isoformat()

        process = None
        log_lines = []
        state = {"seen": False, "current": None, "speed": "", "speed_num": 0.0}

        try:
            creationflags = 0
            if IS_WINDOWS:
                creationflags = subprocess.CREATE_NO_WINDOW

            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,     # 供中止时发送 'q' 优雅退出
                stdout=subprocess.PIPE,    # -progress pipe:1 输出
                stderr=subprocess.PIPE,    # 日志
                creationflags=creationflags,
            )

            with process_lock:
                running_processes[task_id] = process

            stderr_thread = threading.Thread(
                target=_drain_stderr,
                args=(process, task_id, log_lines, state, duration),
                daemon=True,
            )
            stderr_thread.start()

            # -progress pipe:1 输出 key=value 行，据此驱动进度 / 速度 / 剩余时间
            for raw in process.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()

                if key == "out_time":
                    state["seen"] = True
                    state["current"] = parse_time(value)
                    _apply_progress(task_id, state["current"], duration, state)
                elif key in ("out_time_ms", "out_time_us"):
                    if state.get("current") is None:
                        try:
                            state["current"] = float(value) / 1000000.0
                            state["seen"] = True
                            _apply_progress(task_id, state["current"], duration, state)
                        except ValueError:
                            pass
                elif key == "speed":
                    state["speed"] = "" if value in ("N/A", "") else value
                    try:
                        state["speed_num"] = float(value.rstrip("x"))
                    except ValueError:
                        state["speed_num"] = 0.0
                    _apply_progress(task_id, state.get("current"), duration, state)

            process.wait()
            stderr_thread.join(timeout=5)

            with tasks_lock:
                cancelled = tasks[task_id].get("cancelled", False)
                if cancelled:
                    tasks[task_id]["status"] = "cancelled"
                    tasks[task_id]["error"] = "用户已中止任务"
                elif process.returncode == 0:
                    tasks[task_id]["status"] = "completed"
                    tasks[task_id]["progress"] = 100
                else:
                    tasks[task_id]["status"] = "failed"
                    tasks[task_id]["error"] = f"FFmpeg 退出码: {process.returncode}"
                tasks[task_id]["log"] = "\n".join(log_lines[-60:])
                tasks[task_id]["speed"] = ""
                tasks[task_id]["eta"] = None
                tasks[task_id]["end_time"] = datetime.now().isoformat()

        except Exception as e:
            with tasks_lock:
                if tasks[task_id].get("cancelled"):
                    tasks[task_id]["status"] = "cancelled"
                    tasks[task_id]["error"] = "用户已中止任务"
                else:
                    tasks[task_id]["status"] = "failed"
                    tasks[task_id]["error"] = str(e)
                tasks[task_id]["speed"] = ""
                tasks[task_id]["eta"] = None
        finally:
            with process_lock:
                running_processes.pop(task_id, None)


@app.route("/")
def index():
    return render_template("index.html", ffmpeg_path=FFMPEG, media_dir=MEDIA_DIR)


@app.route("/api/browse")
def browse():
    path = request.args.get("path") or MEDIA_DIR
    path = os.path.abspath(os.path.expanduser(path))

    if not os.path.exists(path):
        return jsonify({"error": f"路径不存在: {path}"}), 404
    if not os.path.isdir(path):
        return jsonify({"error": "不是目录"}), 400

    try:
        entries = []
        for entry in os.scandir(path):
            if entry.name.startswith("."):
                continue
            is_dir = entry.is_dir()
            ext = os.path.splitext(entry.name)[1].lower()
            if not is_dir and ext not in VIDEO_EXTS:
                continue
            item = {"name": entry.name, "path": entry.path, "is_dir": is_dir}
            if not is_dir:
                try:
                    item["size"] = entry.stat().st_size
                    item["size_human"] = human_size(item["size"])
                except Exception:
                    pass
            entries.append(item)

        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

        parent = os.path.dirname(path)
        if not parent or os.path.normpath(parent) == os.path.normpath(path):
            parent = None

        return jsonify({"path": path, "parent": parent, "items": entries})
    except PermissionError:
        return jsonify({"error": "没有权限访问该目录"}), 403
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/info")
def info():
    filepath = request.args.get("path", "")
    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "文件不存在"}), 404
    data = get_video_info(filepath)
    if not data or "error" in data:
        return jsonify({"error": data.get("error", "无法读取视频信息") if data else "无法读取视频信息"}), 500

    result = {"format": {}, "video": [], "audio": []}
    fmt = data.get("format", {})
    result["format"] = {
        "format_name": fmt.get("format_long_name", ""),
        "duration": fmt.get("duration", ""),
        "size": fmt.get("size", ""),
        "bit_rate": fmt.get("bit_rate", ""),
    }
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            result["video"].append({
                "codec": s.get("codec_name"),
                "width": s.get("width"), "height": s.get("height"),
                "fps": s.get("r_frame_rate"),
                "bit_rate": s.get("bit_rate"),
                "pix_fmt": s.get("pix_fmt"),
            })
        elif s.get("codec_type") == "audio":
            result["audio"].append({
                "codec": s.get("codec_name"),
                "sample_rate": s.get("sample_rate"),
                "channels": s.get("channels"),
                "bit_rate": s.get("bit_rate"),
            })
    return jsonify(result)


@app.route("/api/stream")
def stream():
    path = request.args.get("path", "")
    if not path or not os.path.exists(path) or not os.path.isfile(path):
        return "Not found", 404
    return send_file(path, conditional=True)


@app.route("/api/transcode", methods=["POST"])
def transcode():
    d = request.json or {}
    input_path = d.get("input", "").strip()
    output_path = d.get("output", "").strip()
    start_time = d.get("start_time", "").strip()
    end_time = d.get("end_time", "").strip()
    head_trim = d.get("head_trim", "").strip()   # 新增：批量片头裁剪
    tail_trim = d.get("tail_trim", "").strip()   # 新增：批量片尾裁剪
    platform_name = d.get("platform", "software")
    fmt = d.get("format", "h264")
    quality_value = d.get("quality_value", "23")
    bitrate = d.get("bitrate", "").strip()
    preset = d.get("preset", "medium")
    scale = d.get("scale", "").strip()
    acodec = d.get("acodec", "copy")
    abitrate = d.get("abitrate", "")
    extra = d.get("extra", "").strip()

    if not input_path or not os.path.exists(input_path):
        return jsonify({"error": "输入文件不存在"}), 400
    if not output_path:
        return jsonify({"error": "请指定输出路径"}), 400
    if same_file(input_path, output_path):
        return jsonify({"error": "输出路径不能和输入相同"}), 400
    if os.path.isdir(output_path):
        return jsonify({"error": "输出路径是已存在的目录"}), 400

    out_ext = output_ext(output_path)
    if not out_ext:
        return jsonify({"error": "输出文件缺少扩展名，请指定如 .mp4 / .mkv"}), 400

    # 覆盖保护：前端确认后带 overwrite=true 再次提交
    if os.path.exists(output_path) and not d.get("overwrite"):
        return jsonify({"error": "输出文件已存在", "exists": True}), 409

    # ===== 参数校验 =====
    try:
        quality_value = int(float(str(quality_value)))
    except (TypeError, ValueError):
        return jsonify({"error": "质量值必须是数字"}), 400
    quality_value = max(0, min(51, quality_value))

    if bitrate and not BITRATE_RE.match(bitrate):
        return jsonify({"error": "目标码率格式不正确，示例：5M / 8000k"}), 400
    if scale and not SCALE_RE.match(scale):
        return jsonify({"error": "缩放格式不正确，示例：1920:1080 或 -2:720"}), 400
    if platform_name in VALID_PRESETS and preset not in VALID_PRESETS[platform_name]:
        preset = DEFAULT_PRESETS[platform_name]

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    # 探测源
    src_info = get_video_info(input_path)
    total_duration = 0.0
    try:
        total_duration = float(src_info["format"]["duration"])
    except Exception:
        pass

    # ===== 时间计算 =====
    # 优先级：绝对时间 (start_time/end_time) > 相对裁剪 (head_trim/tail_trim)
    start_sec = 0.0
    end_sec = 0.0

    if start_time:
        start_sec = parse_time(start_time)
    elif head_trim and head_trim != "00:00:00":
        start_sec = parse_time(head_trim)

    if end_time:
        end_sec = parse_time(end_time)
    elif tail_trim and tail_trim != "00:00:00" and total_duration > 0:
        tail_sec = parse_time(tail_trim)
        end_sec = max(0.0, total_duration - tail_sec)

    # ===== 容器兼容性检查 =====
    if platform_name == "copy":
        err = check_copy_video_compat(out_ext, src_info)
        if err:
            return jsonify({"error": err}), 400
    elif out_ext == "webm" and fmt != "av1":
        return jsonify({"error": "WebM 容器仅支持 VP8 / VP9 / AV1 视频，"
                                 "请改输出扩展名（如 .mp4 / .mkv）或选择 AV1 编码"}), 400

    # ===== 音频兼容性检查 =====
    acodec, abitrate, auto_switched, audio_note = ensure_audio_compatible(
        out_ext, acodec, abitrate, src_info
    )

    cmd = [FFMPEG, "-y", "-hide_banner", "-nostats", "-progress", "pipe:1"]

    pre_input, vf_list, enc_args = [], [], []
    enc = "copy"

    if platform_name != "copy":
        enc, pre_input, vf_list, enc_args = build_encoder(
            platform_name, fmt, quality_value, bitrate, preset, scale
        )
        if enc is None:
            return jsonify({"error": f"不支持的平台/格式组合：{platform_name}/{fmt}"}), 400

    cmd += pre_input

    if start_sec > 0:
        cmd += ["-ss", f"{start_sec:.3f}"]
    cmd += ["-i", input_path]
    if end_sec > start_sec:
        cmd += ["-t", f"{end_sec - start_sec:.3f}"]

    cmd += ["-c:v", enc]

    if vf_list:
        cmd += ["-vf", ",".join(vf_list)]

    cmd += enc_args

    cmd += ["-c:a", acodec]
    if acodec != "copy" and abitrate:
        cmd += ["-b:a", abitrate]

    if extra:
        cmd += extra.split()

    if out_ext in FASTSTART_EXTS:
        cmd += ["-movflags", "+faststart"]
    cmd += [output_path]

    actual_duration = None
    if end_sec > start_sec:
        actual_duration = end_sec - start_sec
    elif total_duration:
        actual_duration = total_duration - start_sec

    task_id = str(uuid.uuid4())
    with tasks_lock:
        _prune_tasks_locked()
        tasks[task_id] = {
            "id": task_id, "status": "pending", "progress": 0,
            "input": input_path, "output": output_path,
            "cmd": " ".join(cmd), "log": "", "error": "",
            "cancelled": False,
            "speed": "", "eta": None,
            "audio_note": audio_note if auto_switched else "",
        }

    threading.Thread(target=run_ffmpeg_task,
                     args=(task_id, cmd, actual_duration), daemon=True).start()
    return jsonify({
        "task_id": task_id,
        "cmd": " ".join(cmd),
        "audio_note": audio_note if auto_switched else "",
    })


@app.route("/api/task/<task_id>")
def task_status(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
        if not task:
            return jsonify({"error": "任务不存在"}), 404
        return jsonify(dict(task))


@app.route("/api/task/<task_id>/stop", methods=["POST"])
def stop_task(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
        if not task:
            return jsonify({"error": "任务不存在"}), 404
        if task["status"] not in ("running", "pending"):
            return jsonify({"error": f"任务当前状态为 {task['status']}，无法中止"}), 400
        task["cancelled"] = True
        if task["status"] == "pending":
            # 还在排队 / 尚未拉起 FFmpeg，直接标记结束
            task["status"] = "cancelled"
            task["error"] = "用户已中止任务"
            task["end_time"] = datetime.now().isoformat()

    with process_lock:
        process = running_processes.get(task_id)

    if not process:
        return jsonify({"ok": True, "message": "任务尚未启动或已结束"})

    try:
        # 先发 'q' 让 FFmpeg 优雅退出（mp4 会正常写完文件尾）
        try:
            if process.stdin:
                process.stdin.write(b"q")
                process.stdin.flush()
        except Exception:
            pass

        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except Exception:
                    pass
        return jsonify({"ok": True, "message": "已中止"})
    except Exception as e:
        return jsonify({"error": f"中止失败: {e}"}), 500


_ffmpeg_check_cache = {"ts": 0.0, "data": None}


@app.route("/api/ffmpeg_check")
def ffmpeg_check():
    # 60 秒内复用结果，避免每次刷新页面都重新探测
    if _ffmpeg_check_cache["data"] is not None and time.time() - _ffmpeg_check_cache["ts"] < 60:
        return jsonify(_ffmpeg_check_cache["data"])

    result = {
        "ok": False,
        "path": FFMPEG,
        "version": "",
        "encoders": {},
        "platform": platform.system(),
        "vaapi_device": False,
    }
    if IS_LINUX:
        result["vaapi_device"] = os.path.exists(VAAPI_DEVICE)

    code, stdout, stderr = run_subprocess([FFMPEG, "-version"], timeout=10)
    result["ok"] = (code == 0)
    result["version"] = stdout.splitlines()[0] if stdout else ""
    if code != 0:
        result["error"] = stderr[:300]

    if result["ok"]:
        code, stdout, stderr = run_subprocess(
            [FFMPEG, "-hide_banner", "-encoders"], timeout=10
        )
        encoders = {}
        for enc_name in ["libx264", "libx265", "libsvtav1",
                         "h264_nvenc", "hevc_nvenc", "av1_nvenc",
                         "h264_qsv", "hevc_qsv", "av1_qsv",
                         "h264_amf", "hevc_amf", "av1_amf",
                         "h264_vaapi", "hevc_vaapi", "av1_vaapi"]:
            if enc_name in stdout:
                encoders[enc_name] = True
        result["encoders"] = encoders

    _ffmpeg_check_cache["ts"] = time.time()
    _ffmpeg_check_cache["data"] = result
    return jsonify(result)


if __name__ == "__main__":
    print(f"[FFmpeg Web] 系统    = {platform.system()} {platform.release()}")
    print(f"[FFmpeg Web] FFMPEG  = {FFMPEG}")
    print(f"[FFmpeg Web] FFPROBE = {FFPROBE}")
    if IS_LINUX:
        print(f"[FFmpeg Web] VAAPI_DEVICE = {VAAPI_DEVICE} (exists={os.path.exists(VAAPI_DEVICE)})")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)