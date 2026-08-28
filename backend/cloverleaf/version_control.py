from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


logger = logging.getLogger(__name__)

LOCAL_EXCLUDES = """# Cloverleaf local build artifacts
*.aux
*.bbl
*.blg
*.fdb_latexmk
*.fls
*.log
*.out
*.pdf
*.synctex.gz
*.toc
.cloverleaf/
"""


@dataclass(frozen=True)
class RepositoryStatus:
    initialized: bool
    head: str | None = None
    last_error: str | None = None


class LocalGitRepository:
    """A project-root Git repository used only for local Cloverleaf checkpoints."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.executable = shutil.which("git")
        self.initialized = False
        self.last_error: str | None = None
        self._lock = threading.Lock()

    def _run(
        self,
        *args: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if self.executable is None:
            raise RuntimeError("git is not installed or is not on PATH")
        result = subprocess.run(
            [self.executable, "-C", str(self.root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(detail or f"git {' '.join(args)} failed")
        return result

    def initialize(self) -> RepositoryStatus:
        with self._lock:
            try:
                if self.executable is None:
                    raise RuntimeError("git is not installed or is not on PATH")
                if not (self.root / ".git").exists():
                    self._run("init", "--quiet", "--initial-branch=main")
                top_level = Path(
                    self._run("rev-parse", "--show-toplevel").stdout.strip()
                ).resolve()
                if top_level != self.root:
                    raise RuntimeError("the project root is not the Git repository root")
                self._install_local_excludes()
                self.initialized = True
                self.last_error = None
                if self._run("rev-parse", "--verify", "HEAD", check=False).returncode != 0:
                    self._checkpoint_locked(["."], "Initial Cloverleaf project checkpoint")
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                self.initialized = False
                self.last_error = str(exc)
                logger.warning("Local Git initialization failed for %s: %s", self.root, exc)
            return self.status()

    def _install_local_excludes(self) -> None:
        exclude_value = self._run("rev-parse", "--git-path", "info/exclude").stdout.strip()
        exclude_path = Path(exclude_value)
        if not exclude_path.is_absolute():
            exclude_path = self.root / exclude_path
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        marker = "# Cloverleaf local build artifacts"
        if marker not in existing:
            prefix = existing.rstrip()
            exclude_path.write_text(
                f"{prefix}\n\n{LOCAL_EXCLUDES}" if prefix else LOCAL_EXCLUDES,
                encoding="utf-8",
            )
            return
        existing_lines = set(existing.splitlines())
        missing_lines = [
            line
            for line in LOCAL_EXCLUDES.splitlines()
            if line and not line.startswith("#") and line not in existing_lines
        ]
        if missing_lines:
            exclude_path.write_text(
                f"{existing.rstrip()}\n" + "\n".join(missing_lines) + "\n",
                encoding="utf-8",
            )

    @staticmethod
    def _pathspecs(paths: list[str]) -> list[str]:
        pathspecs: list[str] = []
        for raw_path in paths:
            if raw_path == ".":
                pathspec = "."
            else:
                pure = PurePosixPath(raw_path.replace("\\", "/"))
                if pure.is_absolute() or any(
                    part in {"", ".", ".."} or part.casefold() == ".git"
                    for part in pure.parts
                ):
                    raise ValueError("checkpoint paths must be relative project paths")
                pathspec = pure.as_posix()
            if pathspec not in pathspecs:
                pathspecs.append(pathspec)
        return pathspecs

    def checkpoint(self, paths: list[str], message: str) -> bool:
        with self._lock:
            if not self.initialized:
                return False
            try:
                committed = self._checkpoint_locked(paths, message)
                self.last_error = None
                return committed
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                self.last_error = str(exc)
                logger.warning("Local Git checkpoint failed for %s: %s", self.root, exc)
                return False

    def _checkpoint_locked(self, paths: list[str], message: str) -> bool:
        pathspecs = self._pathspecs(paths)
        if not pathspecs:
            return False
        added = self._run("add", "-A", "--", *pathspecs, check=False)
        if added.returncode != 0:
            detail = (added.stderr or added.stdout).strip()
            if "did not match any files" in detail or "ignored by one of your .gitignore" in detail:
                return False
            raise RuntimeError(detail or "git add failed")
        staged = self._run("diff", "--cached", "--quiet", "--", *pathspecs, check=False)
        if staged.returncode == 0:
            return False
        if staged.returncode != 1:
            raise RuntimeError((staged.stderr or staged.stdout).strip() or "git diff failed")

        subject = " ".join(message.replace("\n", " ").split())[:200] or "Cloverleaf checkpoint"
        has_head = self._run("rev-parse", "--verify", "HEAD", check=False).returncode == 0
        commit_paths = ["--only", "--", *pathspecs] if has_head else ["--", *pathspecs]
        self._run(
            "-c",
            "user.name=Cloverleaf",
            "-c",
            "user.email=cloverleaf@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            subject,
            *commit_paths,
        )
        return True

    def status(self) -> RepositoryStatus:
        head: str | None = None
        if self.initialized:
            result = self._run("rev-parse", "--short", "HEAD", check=False)
            if result.returncode == 0:
                head = result.stdout.strip() or None
        return RepositoryStatus(
            initialized=self.initialized,
            head=head,
            last_error=self.last_error,
        )
