"""
HTTP 路由层：页面、目录浏览、媒体信息、预览、转码提交、任务查询与中止。

业务逻辑都在 ffmpeg_utils / tasks 里，这一层只做参数解析与响应组装。
"""

import os
import platform
import re
import time

from flask import Blueprint, jsonify, render_template, request, send_file

from config import (BITRATE_RE, DEFAULT_PRESETS, FASTSTART_EXTS, FFMPEG,
                    IS_LINUX, MEDIA_DIR, SCALE_RE, SCAN_MAX_DIRS,
                    SCAN_MAX_FILES, VALID_PRESETS, VAAPI_DEVICE, VIDEO_EXTS)
from ffmpeg_utils import (build_encoder, check_copy_video_compat,
                          ensure_audio_compatible, get_video_info, human_size,
                          output_ext, parse_time, run_subprocess, same_file,
                          scan_videos)
from tasks import manager

bp = Blueprint("main", __name__)


def _normalize_dir_arg(path):
    """展开 ~、把 Windows 盘符简写（如 "D:"）补成 "D:\\"，再取绝对路径"""
    path = os.path.expanduser(path)
    if re.match(r"^[A-Za-z]:$", path):
        path += "\\"
    return os.path.abspath(path)


def _err(message, code=400):
    """统一的错误响应"""
    return jsonify({"error": message}), code


@bp.route("/")
def index():
    return render_template("index.html", ffmpeg_path=FFMPEG, media_dir=MEDIA_DIR)


@bp.route("/api/browse")
def browse():
    path = request.args.get("path") or MEDIA_DIR
    path = _normalize_dir_arg(path)

    if not os.path.exists(path):
        return _err(f"路径不存在: {path}", 404)
    if not os.path.isdir(path):
        return _err("不是目录")

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
        return _err("没有权限访问该目录", 403)
    except Exception as e:
        return _err(str(e), 500)


@bp.route("/api/drives")
def drives():
    """盘符列表：Windows 返回所有存在的盘符，其他系统返回根目录"""
    items = []
    if os.name == "nt":
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            root = f"{letter}:\\"
            try:
                if os.path.exists(root):
                    items.append({"name": f"{letter}:", "path": root})
            except OSError:
                continue
    else:
        items.append({"name": "/", "path": "/"})
    return jsonify({"items": items})


@bp.route("/api/scan")
def scan():
    """递归探查目录下所有视频文件（跳过隐藏目录），供前端"探查"功能使用"""
    path = request.args.get("path") or MEDIA_DIR
    path = _normalize_dir_arg(path)

    if not os.path.exists(path):
        return _err(f"路径不存在: {path}", 404)
    if not os.path.isdir(path):
        return _err("不是目录")

    items, truncated = scan_videos(path, max_files=SCAN_MAX_FILES, max_dirs=SCAN_MAX_DIRS)
    return jsonify({"path": path, "count": len(items), "truncated": truncated, "items": items})


@bp.route("/api/info")
def info():
    filepath = request.args.get("path", "")
    if not filepath or not os.path.exists(filepath):
        return _err("文件不存在", 404)
    data = get_video_info(filepath)
    if not data or "error" in data:
        return _err((data or {}).get("error", "无法读取视频信息"), 500)

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


@bp.route("/api/stream")
def stream():
    path = request.args.get("path", "")
    if not path or not os.path.exists(path) or not os.path.isfile(path):
        return "Not found", 404
    return send_file(path, conditional=True)


@bp.route("/api/transcode", methods=["POST"])
def transcode():
    d = request.json or {}
    input_path = d.get("input", "").strip()
    output_path = d.get("output", "").strip()
    start_time = d.get("start_time", "").strip()
    end_time = d.get("end_time", "").strip()
    head_trim = d.get("head_trim", "").strip()   # 批量片头裁剪
    tail_trim = d.get("tail_trim", "").strip()   # 批量片尾裁剪
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
        return _err("输入文件不存在")
    if not output_path:
        return _err("请指定输出路径")
    if same_file(input_path, output_path):
        return _err("输出路径不能和输入相同")
    if os.path.isdir(output_path):
        return _err("输出路径是已存在的目录")

    out_ext = output_ext(output_path)
    if not out_ext:
        return _err("输出文件缺少扩展名，请指定如 .mp4 / .mkv")

    # 覆盖保护：前端确认后带 overwrite=true 再次提交
    if os.path.exists(output_path) and not d.get("overwrite"):
        return jsonify({"error": "输出文件已存在", "exists": True}), 409

    # ===== 参数校验 =====
    try:
        quality_value = int(float(str(quality_value)))
    except (TypeError, ValueError):
        return _err("质量值必须是数字")
    quality_value = max(0, min(51, quality_value))

    if bitrate and not BITRATE_RE.match(bitrate):
        return _err("目标码率格式不正确，示例：5M / 8000k")
    if scale and not SCALE_RE.match(scale):
        return _err("缩放格式不正确，示例：1920:1080 或 -2:720")
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
            return _err(err)
    elif out_ext == "webm" and fmt != "av1":
        return _err("WebM 容器仅支持 VP8 / VP9 / AV1 视频，"
                    "请改输出扩展名（如 .mp4 / .mkv）或选择 AV1 编码")

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
            return _err(f"不支持的平台/格式组合：{platform_name}/{fmt}")

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

    note = audio_note if auto_switched else ""
    task_id = manager.create_task(
        cmd, actual_duration,
        input_path=input_path, output_path=output_path,
        audio_note=note,
    )
    return jsonify({
        "task_id": task_id,
        "cmd": " ".join(cmd),
        "audio_note": note,
    })


@bp.route("/api/task/<task_id>")
def task_status(task_id):
    task = manager.get(task_id)
    if not task:
        return _err("任务不存在", 404)
    return jsonify(task)


@bp.route("/api/tasks")
def list_tasks():
    return jsonify({"tasks": manager.list_tasks()})


@bp.route("/api/task/<task_id>/stop", methods=["POST"])
def stop_task(task_id):
    code, body = manager.stop(task_id)
    return jsonify(body), code


_ffmpeg_check_cache = {"ts": 0.0, "data": None}


@bp.route("/api/ffmpeg_check")
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
