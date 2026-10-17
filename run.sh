#!/usr/bin/env bash
# 一键启动脚本（Linux / macOS）
# 用法：
#   ./run.sh                      # 默认端口 5000
#   ./run.sh 8080                 # 指定端口
#   MEDIA_DIR=/data/media ./run.sh
#
# 说明：
#   - 自动查找 Python3
#   - 自动创建虚拟环境 .venv 并安装依赖（首次运行需要联网）
#   - 自动检查 FFmpeg 目录下的 ffmpeg / ffprobe
#   - Ctrl+C 即可停止服务
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=============================================="
echo "  FFmpeg Web 转码工具 - 一键启动 (Linux/macOS)"
echo "=============================================="

# ---------- 1. 查找可用的 Python3 ----------
PYTHON=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
            PYTHON="$cand"
            break
        fi
    fi
done
if [ -z "$PYTHON" ]; then
    echo "[错误] 未找到 Python 3.8+，请先安装。"
    echo "       Debian/Ubuntu: sudo apt install python3 python3-venv"
    exit 1
fi
echo "[1/4] 使用 $PYTHON（$("$PYTHON" --version 2>&1)）"

# ---------- 2. 准备虚拟环境 ----------
VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo "[2/4] 创建虚拟环境 .venv ..."
    if ! "$PYTHON" -m venv "$VENV_DIR" 2>/dev/null; then
        echo "[错误] 创建虚拟环境失败。"
        echo "       Debian/Ubuntu 可能需要: sudo apt install python3-venv"
        exit 1
    fi
else
    echo "[2/4] 虚拟环境已存在，跳过创建"
fi

# ---------- 3. 安装依赖 ----------
if ! "$VENV_PY" -c 'import flask' >/dev/null 2>&1; then
    echo "[3/4] 安装依赖中（首次运行需要联网，请稍候）..."
    "$VENV_PY" -m pip install --upgrade pip -q --disable-pip-version-check
    if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
        "$VENV_PY" -m pip install -r "$SCRIPT_DIR/requirements.txt" --disable-pip-version-check
    else
        "$VENV_PY" -m pip install flask --disable-pip-version-check
    fi
else
    echo "[3/4] 依赖已安装，跳过"
fi

# ---------- 4. 检查 FFmpeg 并启动 ----------
FFMPEG_DIR="$SCRIPT_DIR/FFmpeg"
if [ -f "$FFMPEG_DIR/ffmpeg" ]; then
    chmod +x "$FFMPEG_DIR/ffmpeg" "$FFMPEG_DIR/ffprobe" 2>/dev/null || true
elif [ -f "$SCRIPT_DIR/ffmpeg" ]; then
    # 兼容旧布局：可执行文件直接放在项目根目录
    chmod +x "$SCRIPT_DIR/ffmpeg" "$SCRIPT_DIR/ffprobe" 2>/dev/null || true
else
    echo "[警告] FFmpeg 目录没有 ffmpeg / ffprobe，将尝试使用系统 PATH 中的 FFmpeg。"
    echo "       下载: https://johnvansickle.com/ffmpeg/ "
    echo "       解压后复制 ffmpeg、ffprobe 到 FFmpeg 目录并执行: chmod +x FFmpeg/ffmpeg FFmpeg/ffprobe"
fi

if [ -n "${1:-}" ] && [ "$1" -eq "$1" ] 2>/dev/null; then
    export PORT="$1"
fi
PORT_SHOW="${PORT:-5000}"

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')" || true
echo "[4/4] 启动服务：http://localhost:$PORT_SHOW"
if [ -n "$LAN_IP" ]; then
    echo "      局域网访问：http://$LAN_IP:$PORT_SHOW"
fi
echo "      （Ctrl+C 停止）"
echo ""

exec "$VENV_PY" "$SCRIPT_DIR/app.py"
