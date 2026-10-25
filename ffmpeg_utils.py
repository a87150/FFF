"""
FFmpeg / FFprobe 工具层：文件探测、命令构建、容器兼容性检查、进度换算。

这一层不依赖 Flask、不持有全局状态，所有函数都是纯逻辑（除探测/执行外），
方便单元测试。
"""

import json
import os
import re
import subprocess

from config import (CONTAINER_AUDIO_CODECS, CONTAINER_FALLBACK_ACODEC,
                    CONTAINER_VIDEO_CODECS, FASTSTART_EXTS, FFMPEG, FFPROBE,
                    IS_LINUX, VAAPI_DEVICE, VIDEO_EXTS)


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


def scan_videos(root, max_files=2000, max_dirs=20000):
    """
    递归扫描 root 下所有视频文件（跳过隐藏文件 / 隐藏目录）。
    返回 (items, truncated)；items 按相对路径排序，字段：
        name（文件名）/ path（绝对路径）/ rel（相对 root 的路径）/ size / size_human
    """
    items = []
    truncated = False
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root):
        visited += 1
        if visited > max_dirs:
            truncated = True
            break
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            if os.path.splitext(fn)[1].lower() not in VIDEO_EXTS:
                continue
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            items.append({
                "name": fn,
                "path": full,
                "rel": os.path.relpath(full, root),
                "size": size,
                "size_human": human_size(size) if size else "",
            })
            if len(items) >= max_files:
                truncated = True
                break
        if truncated:
            break
    items.sort(key=lambda it: it["rel"].lower())
    return items, truncated


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


# ==================== 合并（把队列里的多个文件按顺序拼成一个） ====================

def merge_stream_signature(info):
    """提取视频 / 音频流的特征，用于判断能否用 copy 直接拼接"""
    video = audio = None
    for s in (info or {}).get("streams", []):
        if s.get("codec_type") == "video" and video is None:
            video = s
        elif s.get("codec_type") == "audio" and audio is None:
            audio = s
    vkey = (video.get("codec_name"), video.get("width"), video.get("height"),
            video.get("pix_fmt"), video.get("r_frame_rate")) if video else None
    akey = (audio.get("codec_name"), audio.get("sample_rate"), audio.get("channels")) if audio else None
    return vkey, akey


def can_merge_copy(infos):
    """所有文件的视频 / 音频流特征完全一致时，才能用 concat demuxer + -c copy 直接合并"""
    if len(infos) < 2:
        return False
    signatures = [merge_stream_signature(i) for i in infos]
    first = signatures[0]
    if not first[0]:
        return False
    return all(sig == first for sig in signatures)


def merge_audio_fallback(out_ext, acodec, abitrate):
    """合并必须重新编码音频：copy / 空值自动换成容器默认编码，返回 (编码, 码率, 提示)"""
    if acodec and acodec != "copy":
        return acodec, abitrate, ""
    fallback = CONTAINER_FALLBACK_ACODEC.get(out_ext)
    if fallback:
        codec, label = fallback
        return codec, abitrate or ("128k" if codec == "libopus" else "192k"), \
               f"合并需要重新编码音频，已自动改为 {label}"
    return "aac", abitrate or "192k", "合并需要重新编码音频，已自动改为 AAC"


def _merge_target(infos, scale):
    """合并目标 (宽, 高, 帧率)：优先用户指定的 scale，否则用第一个文件的尺寸"""
    first = None
    for s in (infos[0] or {}).get("streams", []):
        if s.get("codec_type") == "video":
            first = s
            break
    if not first:
        return None
    try:
        w0, h0 = int(first.get("width")), int(first.get("height"))
    except (TypeError, ValueError):
        return None
    if w0 <= 0 or h0 <= 0:
        return None

    fps = first.get("r_frame_rate") or "30"
    if not re.match(r"^\d+(\.\d+)?/\d+(\.\d+)?$", fps):
        fps = "30"

    scale = (scale or "").strip()
    m = re.match(r"^(\d+):(\d+)$", scale)
    if m:
        return int(m.group(1)), int(m.group(2)), fps
    m = re.match(r"^(-?\d+):(-?\d+)$", scale)
    if m:
        sw, sh = int(m.group(1)), int(m.group(2))
        if sw <= 0 and sh <= 0:
            return w0, h0, fps
        if sw <= 0:
            return max(2, int(round(sh * w0 / h0 / 2.0)) * 2), sh, fps
        if sh <= 0:
            return sw, max(2, int(round(sw * h0 / w0 / 2.0)) * 2), fps
        return sw, sh, fps
    return w0, h0, fps


def build_merge_command(entries, output_path, platform_name, fmt, quality_value,
                        bitrate, preset, scale, acodec="aac", abitrate="",
                        extra="", head_trim="", tail_trim=""):
    """
    构建「重新编码合并」命令（filter_complex + concat 滤镜，自动统一分辨率 / 帧率 / 音频）。
    entries: [{"path": 文件路径, "info": ffprobe 结果}]
    返回 (cmd, total_duration, note)；参数不合理时 cmd 为 None，note 为错误信息。
    """
    if len(entries) < 2:
        return None, 0.0, "合并至少需要 2 个文件"

    infos = [e.get("info") or {} for e in entries]
    target = _merge_target(infos, scale)
    if not target:
        return None, 0.0, "无法读取视频尺寸 / 帧率，不能合并这些文件"
    width, height, fps = target

    enc, pre_input, vf_extra, enc_args = build_encoder(
        platform_name, fmt, quality_value, bitrate, preset, "")
    if enc is None:
        return None, 0.0, f"当前平台不支持合并：{platform_name}/{fmt}"

    out_ext = output_ext(output_path)
    acodec, abitrate, audio_note = merge_audio_fallback(out_ext, acodec, abitrate)

    head = parse_time(head_trim) if head_trim and head_trim != "00:00:00" else 0.0
    tail = parse_time(tail_trim) if tail_trim and tail_trim != "00:00:00" else 0.0

    parts, concat_in = [], []
    total, known_duration = 0.0, True
    for i, entry in enumerate(entries):
        info = entry.get("info") or {}
        try:
            dur = float((info.get("format") or {}).get("duration"))
        except (TypeError, ValueError):
            dur, known_duration = 0.0, False

        start = head
        end = dur - tail if dur > 0 else 0.0
        if dur > 0 and end <= start:
            return None, 0.0, "片头 / 片尾裁剪后时长为 0，请检查裁剪时间"
        seg = (end - start) if dur > 0 else 0.0
        if dur > 0:
            total += seg

        needs_trim = start > 0 or (dur > 0 and end < dur)
        trim_v = f"trim=start={start:.3f}:end={end:.3f}," if needs_trim else ""
        trim_a = f"atrim=start={start:.3f}:end={end:.3f}," if needs_trim else ""
        parts.append(
            f"[{i}:v]{trim_v}setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{i}]")

        has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
        if has_audio:
            parts.append(
                f"[{i}:a]{trim_a}asetpts=PTS-STARTPTS,aresample=48000,"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
        else:
            if dur <= 0:
                return None, 0.0, "合并的文件缺少音频且无法读取时长，不能自动补齐静音"
            parts.append(
                f"anullsrc=r=48000:cl=stereo,atrim=0:{seg:.3f},"
                f"asetpts=PTS-STARTPTS[a{i}]")
        concat_in.append(f"[v{i}][a{i}]")

    parts.append("".join(concat_in) + f"concat=n={len(entries)}:v=1:a=1[vcat][acat]")
    vmap = "[vcat]"
    if vf_extra:   # VAAPI 等需要在最后统一上传到显卡
        parts.append("[vcat]" + ",".join(vf_extra) + "[vout]")
        vmap = "[vout]"

    cmd = [FFMPEG, "-y", "-hide_banner", "-nostats", "-progress", "pipe:1"]
    cmd += pre_input
    for entry in entries:
        cmd += ["-i", entry.get("path", "")]
    cmd += ["-filter_complex", ";".join(parts),
            "-map", vmap, "-map", "[acat]",
            "-c:v", enc] + enc_args
    cmd += ["-c:a", acodec]
    if abitrate:
        cmd += ["-b:a", abitrate]
    if extra:
        cmd += extra.split()
    if out_ext in FASTSTART_EXTS:
        cmd += ["-movflags", "+faststart"]
    cmd += [output_path]

    return cmd, (round(total, 3) if known_duration else None), audio_note


def build_merge_copy_command(output_path, concat_list_path):
    """同编码 copy 合并：concat demuxer 读列表文件，不重新编码（快、无损）"""
    cmd = [FFMPEG, "-y", "-hide_banner", "-nostats", "-progress", "pipe:1",
           "-f", "concat", "-safe", "0", "-i", concat_list_path, "-c", "copy"]
    if output_ext(output_path) in FASTSTART_EXTS:
        cmd += ["-movflags", "+faststart"]
    cmd += [output_path]
    return cmd


def write_concat_list(paths, list_path):
    """写 concat demuxer 的列表文件（统一用 /，并转义单引号）"""
    with open(list_path, "w", encoding="utf-8") as f:
        for p in paths:
            f.write("file '" + p.replace("\\", "/").replace("'", "'\\''") + "'\n")
