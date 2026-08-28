import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _write_recorder(path: Path, command: str) -> None:
    path.write_text(
        "#!/bin/sh\n"
        f'printf \'%s|{command}|%s\\n\' "$PWD" "$*" >> "$ENTRYPOINT_LOG"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_dev_and_run_launches_personal_codex_from_repository_root(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_recorder(bin_dir / "make", "make")
    _write_recorder(bin_dir / "multi-cli", "multi-cli")
    log_path = tmp_path / "commands.log"
    environment = os.environ | {
        "ENTRYPOINT_LOG": str(log_path),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }

    result = subprocess.run(
        [str(ROOT / "dev-and-run")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert log_path.read_text(encoding="utf-8").splitlines() == [
        f"{ROOT}|make|test",
        f"{ROOT}|make|lint",
        f"{ROOT}|make|typecheck",
        f"{ROOT}|make|build",
        f"{ROOT}|make|compile",
        f"{ROOT}|make|test-e2e",
        f"{ROOT}|make|start",
        f"{ROOT}|multi-cli|codex/personal",
    ]
