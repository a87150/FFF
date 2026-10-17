"""
FFmpeg / FFprobe 工具层：文件探测、命令构建、容器兼容性检查、进度换算。

这一层不依赖 Flask、不持有全局状态，所有函数都是纯逻辑（除探测/执行外），
方便单元测试。
"""

import json
import os
import subprocess

from config import (CONTAINER_AUDIO_CODECS, CONTAINER_FALLBACK_ACODEC,
                    CONTAINER_VIDEO_CODECS, FFMPEG, FFPROBE, IS_LINUX,
                    VAAPI_DEVICE)


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
    """把 'HH:MM:SS.ms' / 'MM:SS' / '秒数' 转成浮点秒"""
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


def parse_speed(value):
    """把 -progress 的 speed 值（如 '1.5x' / 'N/A'）转成数字，无法解析返回 0"""
    try:
        return float(str(value).rstrip("x"))
    except (TypeError, ValueError):
        return 0.0


def compute_progress(current, duration):
    """输出时间 -> 进度百分比（封顶 99.9，100 留给真正完成时），未知返回 None"""
    if duration and duration > 0 and current is not None:
        return min(99.9, (current / duration) * 100)
    return None


def compute_eta(current, duration, speed_num):
    """根据剩余时长 / 编码速度估算剩余秒数，未知返回 None"""
    if duration and duration > 0 and current is not None and speed_num > 0:
        return max(0.0, (duration - current) / speed_num)
    return None


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
    """调用 ffprobe 读取媒体信息，失败时返回 {'error': ...}"""
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
    """
    按平台/格式生成编码器参数。
    返回 (编码器名, 输入前参数, 滤镜列表, 编码器参数)；不支持的组合编码器名为 None。
    """
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
