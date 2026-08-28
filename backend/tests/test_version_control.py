from __future__ import annotations

import subprocess
from pathlib import Path

from cloverleaf.version_control import LocalGitRepository


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_new_project_repository_records_an_initial_local_baseline(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.tex").write_text("manuscript", encoding="utf-8")
    (root / ".env").write_text("LOCAL_SETTING=1\n", encoding="utf-8")
    (root / "main.pdf").write_bytes(b"generated")

    repository = LocalGitRepository(root)
    status = repository.initialize()

    assert status.initialized is True
    assert status.head
    assert (root / ".git").is_dir()
    assert git(root, "log", "-1", "--pretty=%s") == "Initial Cloverleaf project checkpoint"
    assert git(root, "ls-files").splitlines() == [".env", "main.tex"]


def test_checkpoint_commits_only_cloverleaf_paths_and_preserves_other_staging(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.tex").write_text("original", encoding="utf-8")
    (root / "notes.txt").write_text("original notes", encoding="utf-8")
    repository = LocalGitRepository(root)
    repository.initialize()

    (root / "notes.txt").write_text("staged notes", encoding="utf-8")
    git(root, "add", "notes.txt")
    (root / "main.tex").write_text("checkpointed", encoding="utf-8")

    assert repository.checkpoint(["main.tex"], "Update main.tex") is True
    assert git(root, "show", "HEAD:main.tex") == "checkpointed"
    assert git(root, "show", "HEAD:notes.txt") == "original notes"
    assert git(root, "diff", "--cached", "--name-only") == "notes.txt"
    assert git(root, "log", "-1", "--pretty=%s") == "Update main.tex"


def test_existing_repository_is_not_swept_into_a_commit_on_activation(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.tex").write_text("original", encoding="utf-8")
    repository = LocalGitRepository(root)
    repository.initialize()
    original_head = git(root, "rev-parse", "HEAD")
    (root / "main.tex").write_text("uncommitted human work", encoding="utf-8")

    restarted = LocalGitRepository(root)
    status = restarted.initialize()

    assert status.initialized is True
    assert git(root, "rev-parse", "HEAD") == original_head
    assert git(root, "status", "--short") == "M main.tex"


def test_existing_cloverleaf_excludes_are_upgraded_for_project_state(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.tex").write_text("original", encoding="utf-8")
    repository = LocalGitRepository(root)
    repository.initialize()
    exclude_path = root / ".git" / "info" / "exclude"
    exclude_path.write_text(
        exclude_path.read_text(encoding="utf-8").replace(".cloverleaf/\n", ""),
        encoding="utf-8",
    )

    LocalGitRepository(root).initialize()

    assert ".cloverleaf/" in exclude_path.read_text(encoding="utf-8").splitlines()
