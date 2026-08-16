import importlib.util
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "cloverleaf_dev_launcher",
    Path(__file__).resolve().parents[2] / "scripts" / "dev.py",
)
assert SPEC is not None and SPEC.loader is not None
dev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dev)


def test_runtime_log_combines_streams_and_rotates(tmp_path: Path) -> None:
    path = tmp_path / "server.log"
    runtime_log = dev.RuntimeLog(path, max_bytes=220)
    runtime_log.write("backend:stdout", "backend ready")
    runtime_log.write("frontend:stderr", "frontend warning")
    runtime_log.write("backend:stderr", "x" * 240)
    runtime_log.close()

    assert "backend:stderr" in path.read_text(encoding="utf-8")
    backup = path.with_name("server.log.1")
    previous = backup.read_text(encoding="utf-8")
    assert "[backend:stdout] backend ready" in previous
    assert "[frontend:stderr] frontend warning" in previous


def test_child_environment_configures_headless_runtime(monkeypatch) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")

    environment = dev.child_environment()

    assert "FORCE_COLOR" not in environment
    assert environment["NO_COLOR"] == "1"
    assert environment["PYTHONUNBUFFERED"] == "1"
    assert environment["CLOVERLEAF_RUNTIME_LOG"] == str(dev.LOG_PATH)
    assert environment["CLOVERLEAF_RUNTIME_SHUTDOWN"] == str(dev.SHUTDOWN_PATH)
