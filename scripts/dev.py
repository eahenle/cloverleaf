"""Run Cloverleaf's backend and frontend under one small local supervisor."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import TextIO


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = ROOT / ".cloverleaf-runtime"
LOG_PATH = RUNTIME_DIR / "server.log"
STATE_PATH = RUNTIME_DIR / "state.json"
LOCK_PATH = RUNTIME_DIR / "launcher.lock"
SHUTDOWN_PATH = RUNTIME_DIR / "shutdown"
MAX_LOG_BYTES = 1_000_000


class RuntimeLog:
    """Thread-safe, bounded combined output from the supervised processes."""

    def __init__(self, path: Path, max_bytes: int = MAX_LOG_BYTES):
        self.path = path
        self.backup_path = path.with_name(f"{path.name}.1")
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("wb", buffering=0)
        self._size = 0

    def write(self, source: str, message: str) -> None:
        line = message.rstrip("\r\n")
        if not line:
            return
        timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        payload = f"{timestamp} [{source}] {line}\n".encode("utf-8", errors="replace")
        with self._lock:
            if self._size and self._size + len(payload) > self.max_bytes:
                self._handle.close()
                self.backup_path.unlink(missing_ok=True)
                self.path.replace(self.backup_path)
                self._handle = self.path.open("wb", buffering=0)
                self._size = 0
            self._handle.write(payload)
            self._size += len(payload)

    def close(self) -> None:
        with self._lock:
            self._handle.close()


def stop(process: subprocess.Popen[str], sig: signal.Signals = signal.SIGTERM) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
    else:
        process.send_signal(sig)


def pump(stream: TextIO, source: str, runtime_log: RuntimeLog) -> None:
    try:
        for line in stream:
            runtime_log.write(source, line)
    finally:
        stream.close()


def process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def load_state() -> dict[str, object] | None:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        pid = int(state["pid"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return state if process_running(pid) else None


def write_state(pid: int) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "pid": pid,
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "url": "http://127.0.0.1:5173",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(STATE_PATH)


def acquire_lock() -> TextIO | None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+", encoding="utf-8")
    if os.name != "posix":
        return handle
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("FORCE_COLOR", None)
    environment.update(
        {
            "CLOVERLEAF_RUNTIME_LOG": str(LOG_PATH),
            "CLOVERLEAF_RUNTIME_SHUTDOWN": str(SHUTDOWN_PATH),
            "NO_COLOR": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


def child_commands() -> list[tuple[str, list[str], Path]]:
    return [
        (
            "backend",
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
                "--timeout-graceful-shutdown",
                "2",
            ],
            ROOT,
        ),
        ("frontend", ["npm", "run", "dev", "--", "--host", "127.0.0.1"], ROOT / "frontend"),
    ]


def supervise(*, foreground: bool) -> int:
    lock_handle = acquire_lock()
    if lock_handle is None:
        if foreground:
            print("Cloverleaf is already running at http://127.0.0.1:5173")
        return 0

    SHUTDOWN_PATH.unlink(missing_ok=True)
    runtime_log = RuntimeLog(LOG_PATH)
    commands = child_commands()
    processes: list[tuple[str, subprocess.Popen[str]]] = []
    readers: list[threading.Thread] = []
    stopping = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopping.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    exit_code = 0
    try:
        for name, command, cwd in commands:
            runtime_log.write("launcher", f"Starting {name}")
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=child_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=os.name == "posix",
            )
            processes.append((name, process))
            assert process.stdout is not None
            assert process.stderr is not None
            for stream_name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                reader = threading.Thread(
                    target=pump,
                    args=(stream, f"{name}:{stream_name}", runtime_log),
                    daemon=True,
                )
                reader.start()
                readers.append(reader)

        write_state(os.getpid())
        runtime_log.write("launcher", "Cloverleaf is available at http://127.0.0.1:5173")
        if foreground:
            print(
                "Cloverleaf is running headlessly at http://127.0.0.1:5173 "
                "(Ctrl-C to stop; logs are in the Terminal tab).",
                flush=True,
            )

        while not stopping.is_set():
            if SHUTDOWN_PATH.exists():
                runtime_log.write("launcher", "Shutdown requested from the Cloverleaf UI")
                stopping.set()
                break
            exited = [(name, process) for name, process in processes if process.poll() is not None]
            if exited:
                name, process = exited[0]
                exit_code = process.returncode or 1
                runtime_log.write("launcher", f"{name} exited with status {exit_code}")
                stopping.set()
                break
            time.sleep(0.2)
    except Exception as exc:
        exit_code = 1
        runtime_log.write("launcher", f"Launcher failed: {exc}")
    finally:
        for _name, process in processes:
            stop(process)
        for name, process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                runtime_log.write("launcher", f"Force-stopping {name}")
                stop(process, signal.SIGKILL)
                process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=1)
        runtime_log.write("launcher", "Cloverleaf stopped")
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if int(state.get("pid", -1)) == os.getpid():
                STATE_PATH.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        SHUTDOWN_PATH.unlink(missing_ok=True)
        runtime_log.close()
        lock_handle.close()
    return exit_code


def backend_ready() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=0.4) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def log_tail() -> str:
    try:
        return LOG_PATH.read_text(encoding="utf-8", errors="replace")[-4_000:]
    except OSError:
        return "No launcher output was captured."


def detach() -> int:
    active = load_state()
    if active is not None:
        print(f"Cloverleaf is already running at {active.get('url', 'http://127.0.0.1:5173')}")
        return 0

    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--supervise"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name == "posix",
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            print(f"Cloverleaf did not start.\n{log_tail()}", file=sys.stderr)
            return process.returncode or 1
        state = load_state()
        if state is not None and int(state["pid"]) == process.pid and backend_ready():
            time.sleep(0.2)
            if process.poll() is None:
                print("Cloverleaf started headlessly at http://127.0.0.1:5173")
                return 0
        time.sleep(0.1)

    stop(process)
    print(f"Cloverleaf timed out during startup.\n{log_tail()}", file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detach", action="store_true", help="start the supervisor in the background")
    parser.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.detach:
        return detach()
    return supervise(foreground=not arguments.supervise)


if __name__ == "__main__":
    raise SystemExit(main())
