"""
TaskManager 行为测试：成功/失败/进度解析/并发排队/中止。

用 `python -c` 模拟 FFmpeg 子进程，不依赖真实 FFmpeg 二进制；
所有等待超时都收紧到亚秒级，整个文件几秒内跑完。
"""

import sys
import time

import pytest

from tasks import TaskManager

PY = sys.executable


def make_manager(**kwargs):
    kwargs.setdefault("is_windows", False)
    kwargs.setdefault("stop_grace_timeout", 0.3)
    kwargs.setdefault("stop_kill_timeout", 0.5)
    return TaskManager(**kwargs)


def wait_status(mgr, task_id, statuses, timeout=15.0):
    """轮询直到任务达到指定状态之一（或超时），返回任务快照"""
    deadline = time.time() + timeout
    task = mgr.get(task_id)
    while time.time() < deadline:
        task = mgr.get(task_id)
        if task and task["status"] in statuses:
            return task
        time.sleep(0.05)
    return task


def wait_process_started(mgr, task_id, timeout=5.0):
    """等子进程真正注册进进程表（running 状态置位后还有极短窗口）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mgr.process_started(task_id):
            return True
        time.sleep(0.02)
    return False


class TestLifecycle:
    def test_complete_ok(self):
        mgr = make_manager()
        tid = mgr.create_task([PY, "-c", "pass"], None, "in.mp4", "out.mp4")
        task = wait_status(mgr, tid, {"completed", "failed", "cancelled"})
        assert task["status"] == "completed"
        assert task["progress"] == 100
        assert task["error"] == ""
        assert task["input"] == "in.mp4" and task["output"] == "out.mp4"

    def test_failed_exit_code(self):
        mgr = make_manager()
        tid = mgr.create_task([PY, "-c", "import sys; sys.exit(3)"])
        task = wait_status(mgr, tid, {"completed", "failed", "cancelled"})
        assert task["status"] == "failed"
        assert "3" in task["error"]

    def test_unknown_task(self):
        mgr = make_manager()
        assert mgr.get("missing") is None

    def test_progress_and_speed(self):
        mgr = make_manager()
        code = ("import sys, time;"
                "print('out_time=00:00:02.000000');"
                "print('speed=4.0x');"
                "print('progress=continue');"
                "sys.stdout.flush();"
                "time.sleep(0.6)")
        tid = mgr.create_task([PY, "-c", code], 10.0, "in.mp4", "out.mp4")

        mid = None
        deadline = time.time() + 10
        while time.time() < deadline:
            t = mgr.get(tid)
            if t["status"] == "running" and t.get("speed"):
                mid = t
                break
            time.sleep(0.05)
        assert mid is not None, "未能在运行中采集到进度/速度"
        assert mid["progress"] == pytest.approx(20.0, abs=0.5)
        assert mid["speed"] == "4.0x"
        assert mid["eta"] == pytest.approx(2.0, abs=0.5)

        task = wait_status(mgr, tid, {"completed", "failed", "cancelled"})
        assert task["status"] == "completed"
        assert task["speed"] == "" and task["eta"] is None


class TestConcurrency:
    def test_second_task_waits_then_runs(self):
        mgr = make_manager(max_concurrent=1)
        t1 = mgr.create_task([PY, "-c", "import time; time.sleep(0.8)"])
        t2 = mgr.create_task([PY, "-c", "pass"])
        time.sleep(0.3)
        assert mgr.get(t1)["status"] == "running"
        assert mgr.get(t2)["status"] == "pending"
        t2_final = wait_status(mgr, t2, {"completed", "failed", "cancelled"})
        assert t2_final["status"] == "completed"

    def test_stop_pending_task_never_runs(self):
        mgr = make_manager(max_concurrent=1)
        t1 = mgr.create_task([PY, "-c", "import time; time.sleep(1.5)"])
        t2 = mgr.create_task([PY, "-c", "pass"])
        wait_status(mgr, t1, {"running"})
        deadline = time.time() + 5
        while time.time() < deadline and mgr.get(t2)["status"] != "pending":
            time.sleep(0.05)
        assert mgr.get(t2)["status"] == "pending"

        code, body = mgr.stop(t2)
        assert code == 200
        assert mgr.get(t2)["status"] == "cancelled"

        # 第一个任务结束后，被中止的排队任务不应被执行
        assert wait_status(mgr, t1, {"completed", "failed", "cancelled"})["status"] == "completed"
        time.sleep(0.3)
        assert mgr.get(t2)["status"] == "cancelled"


class TestStop:
    def test_stop_running_task(self):
        mgr = make_manager()
        tid = mgr.create_task([PY, "-c", "import time; time.sleep(20)"])
        wait_status(mgr, tid, {"running"})
        assert wait_process_started(mgr, tid)
        code, _ = mgr.stop(tid)
        assert code == 200
        task = wait_status(mgr, tid, {"cancelled", "failed"}, timeout=10)
        assert task["status"] == "cancelled"

    def test_stop_finished_task_rejected(self):
        mgr = make_manager()
        tid = mgr.create_task([PY, "-c", "pass"])
        wait_status(mgr, tid, {"completed", "failed"})
        code, _ = mgr.stop(tid)
        assert code == 400

    def test_stop_unknown_task(self):
        mgr = make_manager()
        code, _ = mgr.stop("nope")
        assert code == 404

    def test_kill_all(self):
        mgr = make_manager()
        tid = mgr.create_task([PY, "-c", "import time; time.sleep(20)"])
        wait_status(mgr, tid, {"running"})
        assert wait_process_started(mgr, tid)
        mgr.kill_all()
        task = wait_status(mgr, tid, {"failed", "cancelled"}, timeout=10)
        assert task["status"] in ("failed", "cancelled")


class TestListTasks:
    def test_summaries_include_paths(self):
        mgr = make_manager()
        tid = mgr.create_task([PY, "-c", "import time; time.sleep(0.2)"],
                              duration=1, input_path="in.mp4", output_path="out.mp4")
        items = mgr.list_tasks()
        me = [t for t in items if t["id"] == tid]
        assert len(me) == 1
        assert set(me[0].keys()) == {"id", "status", "input", "output"}
        assert me[0]["input"] == "in.mp4"
        assert me[0]["output"] == "out.mp4"
        assert me[0]["status"] in ("pending", "running", "completed")
        # 不影响任务本身的正常结束
        assert wait_status(mgr, tid, {"completed", "failed"})["status"] == "completed"


class TestPruning:
    def test_old_finished_tasks_pruned(self):
        mgr = make_manager(max_tasks_kept=3)
        ids = []
        for _ in range(5):
            tid = mgr.create_task([PY, "-c", "pass"])
            wait_status(mgr, tid, {"completed", "failed"})
            ids.append(tid)
        # 最多保留 3 条（可能含 1 条正在收尾的，放宽到 4）
        kept = sum(1 for tid in ids if mgr.get(tid) is not None)
        assert kept <= 4
        assert mgr.get(ids[-1]) is not None
