"""
FFmpeg Web 转码工具入口。

模块拆分：
    config.py       全局配置、容器兼容表、参数白名单
    ffmpeg_utils.py 探测 / 命令构建 / 兼容性检查 / 进度换算（纯逻辑）
    tasks.py        任务管理与子进程生命周期（TaskManager）
    routes.py       Flask 路由（蓝图）

启动：
    python app.py                  # 内置开发服务器（threaded）
    USE_WAITRESS=1 python app.py   # 可选 waitress 生产服务器（pip install waitress）
"""

import os
import platform

from flask import Flask

import routes
from config import FFMPEG, FFPROBE, IS_LINUX, MAX_CONCURRENT_TASKS, VAAPI_DEVICE


def create_app():
    app = Flask(__name__)
    app.register_blueprint(routes.bp)
    return app


app = create_app()


if __name__ == "__main__":
    print(f"[FFmpeg Web] 系统    = {platform.system()} {platform.release()}")
    print(f"[FFmpeg Web] FFMPEG  = {FFMPEG}")
    print(f"[FFmpeg Web] FFPROBE = {FFPROBE}")
    if IS_LINUX:
        print(f"[FFmpeg Web] VAAPI_DEVICE = {VAAPI_DEVICE} (exists={os.path.exists(VAAPI_DEVICE)})")

    port = int(os.environ.get("PORT", 5000))
    host = "0.0.0.0"
    threads = max(4, MAX_CONCURRENT_TASKS * 4)

    if os.environ.get("USE_WAITRESS") == "1":
        try:
            from waitress import serve
        except ImportError:
            print("[FFmpeg Web] 未安装 waitress（pip install waitress），回退到内置开发服务器")
        else:
            print(f"[FFmpeg Web] 服务器  = waitress（线程数 {threads}）")
            serve(app, host=host, port=port, threads=threads)
            raise SystemExit(0)

    app.run(host=host, port=port, threaded=True, debug=False)
