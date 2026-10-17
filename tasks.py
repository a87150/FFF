"""
转码任务管理：并发控制、子进程生命周期、进度跟踪、中止处理。

TaskManager 实例是线程安全的，且不依赖 Flask；并发数、等待超时等都可注入，
方便单元测试。模块底部提供一个供 Web 使用的单例 manager。
"""

import atexit
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime

from config import IS_WINDOWS, MAX_CONCURRENT_TASKS, MAX_TASKS_KEPT
from ffmpeg_utils import (compute_eta, compute_progress, parse_speed,
                          parse_time)

# 老版本 FFmpeg 的 stderr 进度回退解析
_TIME_PATTERN = re.compile(rb"time=(\d+):(\d+):(\d+\.\d+)")


class TaskManager:
    def __init__(self, max_concurrent=1, max_tasks_kept=200, is_windows=None,
                 stop_grace_timeout=5.0, stop_kill_timeout=3.0):
        self._tasks = {}
        self._tasks_lock = threading.Lock()
        self._processes = {}
        self._process_lock = threading.Lock()
        self._semaphore = threading.Semaphore(max(1, int(max_concurrent)))
        self._max_tasks_kept = max(1, int(max_tasks_kept))
        self._is_windows = IS_WINDOWS if is_windows is None else bool(is_windows)
        self._stop_grace_timeout = stop_grace_timeout
        self._stop_kill_timeout = stop_kill_timeout
        # stop() 等待“running 但进程还没注册”窗口的最长时间
        self._stop_registration_wait = 1.0

    # ---------- 公开接口 ----------

    def create_task(self, cmd, duration=None, input_path="", output_path="", audio_note=""):
        """登记任务并启动后台线程（超出并发上限时任务保持 pending 排队）"""
        task_id = str(uuid.uuid4())
        with self._tasks_lock:
            self._prune_locked()
            self._tasks[task_id] = {
                "id": task_id, "status": "pending", "progress": 0,
                "input": input_path, "output": output_path,
                "cmd": " ".join(cmd), "log": "", "error": "",
                "cancelled": False,
                "speed": "", "eta": None,
                "audio_note": audio_note or "",
            }
        threading.Thread(target=self._run, args=(task_id, cmd, duration), daemon=True).start()
        return task_id

    def get(self, task_id):
        """返回任务快照（副本），不存在返回 None"""
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            return dict(task) if task else None

    def list_tasks(self):
        """任务摘要列表（含输入/输出路径），供前端刷新后找回进行中的任务"""
        with self._tasks_lock:
            return [
                {"id": t["id"], "status": t["status"],
                 "input": t.get("input", ""), "output": t.get("output", "")}
                for t in self._tasks.values()
            ]

    def process_started(self, task_id):
        """子进程是否已注册（处于 running 且 Popen 已完成）"""
        with self._process_lock:
            return task_id in self._processes

    def stop(self, task_id):
        """
        中止任务：排队中的直接标记结束；运行中的先发 'q' 优雅退出，
        超时才强制终止。返回 (http_code, body)。
        """
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if not task:
                return 404, {"error": "任务不存在"}
            if task["status"] not in ("running", "pending"):
                return 400, {"error": f"任务当前状态为 {task['status']}，无法中止"}
            task["cancelled"] = True
            if task["status"] == "pending":
                # 还在排队 / 尚未拉起 FFmpeg，直接标记结束
                task["status"] = "cancelled"
                task["error"] = "用户已中止任务"
                task["end_time"] = datetime.now().isoformat()

        with self._process_lock:
            process = self._processes.get(task_id)

        if not process:
            # 任务可能刚标记 running、子进程尚未注册，短暂等待以避免漏杀
            deadline = time.time() + self._stop_registration_wait
            while time.time() < deadline:
                with self._process_lock:
                    process = self._processes.get(task_id)
                if process:
                    break
                time.sleep(0.02)

        if not process:
            # 进程压根没起来（或已退出），工作线程会根据 cancelled 标记收尾
            return 200, {"ok": True, "message": "任务尚未启动或已结束"}

        try:
            self._terminate_process(process)
            return 200, {"ok": True, "message": "已中止"}
        except Exception as e:
            return 500, {"error": f"中止失败: {e}"}

    def kill_all(self):
        """进程退出时清理残留的 FFmpeg 子进程"""
        with self._process_lock:
            procs = list(self._processes.values())
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass

    # ---------- 内部实现 ----------

    def _prune_locked(self):
        """超出上限时清理最早的已结束任务（调用方需持有 self._tasks_lock）"""
        overflow = len(self._tasks) - self._max_tasks_kept
        if overflow <= 0:
            return
        for tid in list(self._tasks.keys()):
            if overflow <= 0:
                break
            if self._tasks[tid].get("status") not in ("pending", "running"):
                del self._tasks[tid]
                overflow -= 1

    def _terminate_process(self, process):
        # 先发 'q' 让 FFmpeg 优雅退出（mp4 会正常写完文件尾）
        try:
            if process.stdin:
                process.stdin.write(b"q")
                process.stdin.flush()
        except Exception:
            pass

        try:
            process.wait(timeout=self._stop_grace_timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=self._stop_kill_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except Exception:
                    pass

    def _apply_progress(self, task_id, current, duration, state):
        """根据当前输出时间更新任务进度 / 速度 / 剩余时间估计"""
        progress = compute_progress(current, duration)
        eta = compute_eta(current, duration, state.get("speed_num") or 0.0)

        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            if progress is not None:
                task["progress"] = round(progress, 2)
            if state.get("speed"):
                task["speed"] = state["speed"]
            if eta is not None:
                task["eta"] = round(eta, 1)

    def _drain_stderr(self, process, task_id, log_lines, state, duration):
        """后台线程：持续读取 FFmpeg stderr 作为日志（老版本 FFmpeg 的回退进度解析）"""
        for raw in process.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            log_lines.append(line)
            if len(log_lines) > 300:
                del log_lines[:-300]

            # 没有 -progress 输出时（极老版本 FFmpeg），回退解析 stderr 的 time=
            if not state.get("seen") and duration and duration > 0:
                m = _TIME_PATTERN.search(raw)
                if m:
                    h, mi, s = m.groups()
                    current = int(h) * 3600 + int(mi) * 60 + float(s)
                    state["current"] = current
                    self._apply_progress(task_id, current, duration, state)

            with self._tasks_lock:
                task = self._tasks.get(task_id)
                if task is not None:
                    task["log"] = "\n".join(log_lines[-25:])

    def _run(self, task_id, cmd, duration):
        # 并发上限：超出的任务在这里排队等待（任务状态保持 pending）
        with self._semaphore:
            with self._tasks_lock:
                task = self._tasks.get(task_id)
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
                if self._is_windows:
                    creationflags = subprocess.CREATE_NO_WINDOW

                process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,     # 供中止时发送 'q' 优雅退出
                    stdout=subprocess.PIPE,    # -progress pipe:1 输出
                    stderr=subprocess.PIPE,    # 日志
                    creationflags=creationflags,
                )

                with self._process_lock:
                    self._processes[task_id] = process

                # stop() 可能在进程注册前就已发起，这里补查一次，避免漏杀
                with self._tasks_lock:
                    cancelled_now = self._tasks[task_id].get("cancelled", False)
                if cancelled_now:
                    self._terminate_process(process)

                stderr_thread = threading.Thread(
                    target=self._drain_stderr,
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
                        self._apply_progress(task_id, state["current"], duration, state)
                    elif key in ("out_time_ms", "out_time_us"):
                        if state.get("current") is None:
                            try:
                                state["current"] = float(value) / 1000000.0
                                state["seen"] = True
                                self._apply_progress(task_id, state["current"], duration, state)
                            except ValueError:
                                pass
                    elif key == "speed":
                        state["speed"] = "" if value in ("N/A", "") else value
                        state["speed_num"] = parse_speed(value)
                        self._apply_progress(task_id, state.get("current"), duration, state)

                process.wait()
                stderr_thread.join(timeout=5)

                with self._tasks_lock:
                    task = self._tasks[task_id]
                    if task.get("cancelled"):
                        task["status"] = "cancelled"
                        task["error"] = "用户已中止任务"
                    elif process.returncode == 0:
                        task["status"] = "completed"
                        task["progress"] = 100
                    else:
                        task["status"] = "failed"
                        task["error"] = f"FFmpeg 退出码: {process.returncode}"
                    task["log"] = "\n".join(log_lines[-60:])
                    task["speed"] = ""
                    task["eta"] = None
                    task["end_time"] = datetime.now().isoformat()

            except Exception as e:
                with self._tasks_lock:
                    task = self._tasks[task_id]
                    if task.get("cancelled"):
                        task["status"] = "cancelled"
                        task["error"] = "用户已中止任务"
                    else:
                        task["status"] = "failed"
                        task["error"] = str(e)
                    task["speed"] = ""
                    task["eta"] = None
            finally:
                with self._process_lock:
                    self._processes.pop(task_id, None)


# Web 层使用的单例
manager = TaskManager(max_concurrent=MAX_CONCURRENT_TASKS, max_tasks_kept=MAX_TASKS_KEPT)
atexit.register(manager.kill_all)
