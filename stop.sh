#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="$project_dir/.venv/bin/python"
if [[ ! -x "$python" ]]; then
    python=python3
fi

exec "$python" - "$project_dir" <<'PY'
import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

project = Path(sys.argv[1]).resolve()
runtime = project / "data/runtime"


def owns_lock(process, pid, lock):
    info = os.fstat(lock.fileno())
    device = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}:{info.st_ino}"
    expected = ["FLOCK", "ADVISORY", "WRITE", str(pid), device]
    for fdinfo in (process / "fdinfo").iterdir():
        try:
            for line in fdinfo.read_text().splitlines():
                if line.startswith("lock:") and line.split()[2:7] == expected:
                    return True
        except FileNotFoundError:
            continue
    return False


try:
    lock_fd = os.open(runtime / "runtime.lock", os.O_RDONLY | os.O_NOFOLLOW)
except FileNotFoundError:
    print("mem-plugin 未运行。")
    sys.exit(0)
except OSError:
    sys.exit("无法读取 mem-plugin 的运行锁。")

with os.fdopen(lock_fd, "rb") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        print("mem-plugin 已停止。")
        sys.exit(0)

    try:
        state = json.loads((runtime / "runtime.json").read_text())
        pid = state["pid"]
        if type(pid) is not int or pid <= 1 or state["status"] not in {"starting", "ready"}:
            raise ValueError
        pid_fd = None
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            pid_fd = os.pidfd_open(pid)
        try:
            process = Path("/proc") / str(pid)
            args = (process / "cmdline").read_bytes().split(b"\0")
            current_dir = (process / "cwd").resolve(strict=True)
            module_start = b"-m" in args and args[args.index(b"-m") + 1:][:2] == [
                b"mem_plugin", b"start"
            ]
            if current_dir != project or not module_start or not owns_lock(process, pid, lock):
                raise ValueError

            print(f"正在停止 mem-plugin（PID {pid}）...", flush=True)
            if pid_fd is None:
                os.kill(pid, signal.SIGTERM)
            else:
                signal.pidfd_send_signal(pid_fd, signal.SIGTERM)
            # The supervisor holds the lock until its children have shut down.
            deadline = time.monotonic() + 55
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    time.sleep(0.1)
                else:
                    break
            else:
                sys.exit(f"等待停止超时，请检查 {runtime / 'supervisor.log'}。")
        finally:
            if pid_fd is not None:
                os.close(pid_fd)
    except PermissionError:
        sys.exit("没有权限停止 mem-plugin，请在启动服务的同一用户环境中执行。")
    except (KeyError, OSError, TypeError, ValueError):
        sys.exit("无法确认记录中的进程持有 mem-plugin 运行锁，请检查 data/runtime/runtime.json。")

print("mem-plugin 已停止。")
PY
