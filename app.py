import os
import re
import json
import shutil
import platform
import subprocess
import threading
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

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm",
              ".ts", ".m2ts", ".mpg", ".mpeg", ".vob", ".m4v", ".3gp", ".rmvb"}

# ===== 容器音频兼容性表 =====
# MP4 支持的音频编码
MP4_AUDIO_CODECS = {"aac", "mp3", "ac3", "eac3", "alac", "opus"}
# WebM 支持的音频编码
WEBM_AUDIO_CODECS = {"opus", "vorbis"}
# MKV 几乎支持所有，无需检查


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


def ensure_audio_compatible(output_path, acodec, abitrate, src_info):
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

    out_ext = output_path.lower().rsplit(".", 1)[-1] if "." in output_path else ""

    if out_ext == "mp4" and src_acodec not in MP4_AUDIO_CODECS:
        return "aac", (abitrate or "192k"), True, \
               f"源音频 {src_acodec} 不能封装进 MP4，已自动改为 AAC 重编码"
    if out_ext == "webm" and src_acodec not in WEBM_AUDIO_CODECS:
        return "libopus", (abitrate or "128k"), True, \
               f"源音频 {src_acodec} 不能封装进 WebM，已自动改为 Opus 重编码"
    # MKV / 其他容器通常都兼容
    return acodec, abitrate, False, ""


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
        if scale:
            vf.append(f"scale={scale}")

    elif platform_name == "vaapi":
        enc = {"h264": "h264_vaapi", "h265": "hevc_vaapi", "av1": "av1_vaapi"}.get(fmt)
        if not enc:
            return None, [], [], []
        pre += ["-vaapi_device", VAAPI_DEVICE]
        vf.append("format=nv12")
        if scale:
            vf.append(f"scale_vaapi={scale}")
        vf.append("hwupload")
        enc_args += ["-b:v", bitrate] if use_bitrate else ["-qp", str(quality_value)]

    else:
        return None, [], [], []

    return enc, pre, vf, enc_args


def run_ffmpeg_task(task_id, cmd, duration=None):
    with tasks_lock:
        tasks[task_id]["status"] = "running"
        tasks[task_id]["start_time"] = datetime.now().isoformat()

    process = None
    try:
        creationflags = 0
        if IS_WINDOWS:
            creationflags = subprocess.CREATE_NO_WINDOW

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=creationflags,
        )

        with process_lock:
            running_processes[task_id] = process

        time_pattern = re.compile(rb"time=(\d+):(\d+):(\d+\.\d+)")
        log_lines = []

        for raw in iter(process.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            log_lines.append(line)
            if len(log_lines) > 300:
                log_lines = log_lines[-300:]
            m = time_pattern.search(raw)
            if m and duration and duration > 0:
                h, mi, s = m.groups()
                current = int(h) * 3600 + int(mi) * 60 + float(s)
                progress = min(99.9, (current / duration) * 100)
                with tasks_lock:
                    tasks[task_id]["progress"] = round(progress, 2)
                    tasks[task_id]["log"] = "\n".join(log_lines[-25:])
            else:
                with tasks_lock:
                    tasks[task_id]["log"] = "\n".join(log_lines[-25:])

        process.wait()

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
            tasks[task_id]["end_time"] = datetime.now().isoformat()

    except Exception as e:
        with tasks_lock:
            if tasks[task_id].get("cancelled"):
                tasks[task_id]["status"] = "cancelled"
                tasks[task_id]["error"] = "用户已中止任务"
            else:
                tasks[task_id]["status"] = "failed"
                tasks[task_id]["error"] = str(e)
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
    if os.path.abspath(input_path) == os.path.abspath(output_path):
        return jsonify({"error": "输出路径不能和输入相同"}), 400

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

    # ===== 音频兼容性检查 =====
    acodec, abitrate, auto_switched, audio_note = ensure_audio_compatible(
        output_path, acodec, abitrate, src_info
    )

    cmd = [FFMPEG, "-y", "-hide_banner"]

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

    if output_path.lower().endswith(".mp4"):
        cmd += ["-movflags", "+faststart"]
    cmd += [output_path]

    actual_duration = None
    if end_sec > start_sec:
        actual_duration = end_sec - start_sec
    elif total_duration:
        actual_duration = total_duration - start_sec

    task_id = str(uuid.uuid4())
    with tasks_lock:
        tasks[task_id] = {
            "id": task_id, "status": "pending", "progress": 0,
            "input": input_path, "output": output_path,
            "cmd": " ".join(cmd), "log": "", "error": "",
            "cancelled": False,
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

    with process_lock:
        process = running_processes.get(task_id)

    if not process:
        return jsonify({"ok": True, "message": "任务尚未启动或已结束"})

    try:
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


@app.route("/api/ffmpeg_check")
def ffmpeg_check():
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

    return jsonify(result)


if __name__ == "__main__":
    print(f"[FFmpeg Web] 系统    = {platform.system()} {platform.release()}")
    print(f"[FFmpeg Web] FFMPEG  = {FFMPEG}")
    print(f"[FFmpeg Web] FFPROBE = {FFPROBE}")
    if IS_LINUX:
        print(f"[FFmpeg Web] VAAPI_DEVICE = {VAAPI_DEVICE} (exists={os.path.exists(VAAPI_DEVICE)})")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)