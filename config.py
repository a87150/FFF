"""
全局配置：路径探测、环境变量、容器兼容表、参数校验规则。

所有模块都从这里读取配置；这里不 import 其他项目模块，避免循环依赖。
"""

import os
import platform
import re
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"


def find_executable(name):
    """优先找项目目录下自带的可执行文件，其次找系统 PATH"""
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

# 同时运行的 FFmpeg 任务数上限（超出的任务先排队），可用环境变量调整
try:
    MAX_CONCURRENT_TASKS = max(1, int(os.environ.get("MAX_CONCURRENT_TASKS", "1")))
except ValueError:
    MAX_CONCURRENT_TASKS = 1

# 任务记录最多保留条数（防止长时间运行后内存持续增长）
MAX_TASKS_KEPT = 200

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
