"""Run the backend and frontend together, stopping both when either exits."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def stop(process: subprocess.Popen[bytes], sig: signal.Signals = signal.SIGTERM) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
    else:
        process.send_signal(sig)


def main() -> int:
    commands = [
        (
            [
                sys.executable,
                "-m",
                "uvicorn",
                "cloverleaf.main:app",
                "--app-dir",
                "backend",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--reload",
            ],
            ROOT,
        ),
        (["npm", "run", "dev", "--", "--host", "127.0.0.1"], ROOT / "frontend"),
    ]
    processes = [
        subprocess.Popen(command, cwd=cwd, start_new_session=os.name == "posix")
        for command, cwd in commands
    ]
    stopping = threading.Event()

    def shutdown(_signum: int, _frame: object) -> None:
        stopping.set()
        for process in processes:
            stop(process)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        while not stopping.is_set() and all(process.poll() is None for process in processes):
            time.sleep(0.2)
    finally:
        for process in processes:
            stop(process)
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stop(process, signal.SIGKILL)
                process.wait(timeout=5)
    if stopping.is_set():
        return 0
    return next((process.returncode or 0 for process in processes if process.returncode), 0)


if __name__ == "__main__":
    raise SystemExit(main())
