"""Synchronize project sources with a shared Git repository without rewriting project history."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import subprocess
import threading
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
BUILD_SUFFIXES = (".aux", ".bbl", ".blg", ".fdb_latexmk", ".fls", ".log", ".out", ".synctex.gz", ".toc")
PRIVATE_DIRS = {".git", ".cloverleaf", ".cloverleaf-runtime", ".aws", ".ssh", ".venv", "node_modules", "__pycache__"}


def source_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts) and not PurePosixPath(path).is_absolute() and not any(
        part in PRIVATE_DIRS or part == ".." for part in parts
    ) and not any(
        part == ".env" or part.startswith(".env.") or part in {".DS_Store", "auth.json", "credentials.json"}
        or part.startswith(".cloverleaf-project.json") for part in parts
    ) and not path.endswith(BUILD_SUFFIXES + (".pyc", ".pem", ".key"))


def digest(content: bytes | None) -> str | None:
    return hashlib.sha256(content).hexdigest() if content is not None else None


class ProjectSync:
    def __init__(self, remote: str, state_dir: Path, discovery_root: Path, interval: float = 30):
        self.remote = remote
        self.state_dir = state_dir.resolve()
        self.repository = self.state_dir / "repository"
        self.discovery_root = discovery_root.resolve()
        self.interval = interval
        self._lock = threading.RLock()
        self.last_error: str | None = None
        self.last_synced: str | None = None
        self.projects: dict[str, dict] = {}
        state = self.state_dir / "state.json"
        if state.exists():
            self.projects = json.loads(state.read_text())["projects"]
        self._stop = asyncio.Event()

    def status(self) -> dict:
        return {"enabled": bool(self.remote), "remote": self.remote,
                "last_synced": self.last_synced, "last_error": self.last_error,
                "projects": sorted(self.projects)}

    def _save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_dir / "state.tmp"
        temporary.write_text(json.dumps({"projects": self.projects}, indent=2) + "\n")
        temporary.replace(self.state_dir / "state.json")

    @contextmanager
    def _process_lock(self):
        # Reloaded backends and explicit sync invocations share the same cache.
        import fcntl
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "sync.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def register(self, root: Path) -> None:
        if not self.remote:
            return
        root = root.resolve()
        name = root.name
        if not name or name.startswith(".") or "\\" in name:
            raise ValueError("Project sync requires a visible project directory name")
        with self._lock:
            existing = self.projects.get(name)
            if existing and Path(existing["root"]) != root:
                raise ValueError(f"Project sync name collision: {name}")
            if not existing:
                self.projects[name] = {"root": str(root), "baseline": {}}
                self._save()

    def discover(self) -> None:
        if not self.remote:
            return
        for root in sorted(self.discovery_root.iterdir()):
            if root.is_dir() and not root.is_symlink() and not root.name.startswith(".") and (root / "main.tex").is_file():
                self.register(root)

    @staticmethod
    def _git(root: Path, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace").strip() or "Git sync failed")
        return result.stdout.decode()

    @staticmethod
    def _target(root: Path, relative: str) -> Path:
        if not source_path(relative):
            raise RuntimeError(f"Unsafe sync path: {relative}")
        target = root / relative
        cursor = target
        while cursor != root:
            if cursor.is_symlink():
                raise RuntimeError(f"Sync path contains a symlink: {relative}")
            cursor = cursor.parent
        if not target.resolve().is_relative_to(root.resolve()):
            raise RuntimeError(f"Sync path escapes project: {relative}")
        return target

    def _files(self, root: Path, *, git_root: Path | None = None) -> dict[str, bytes]:
        if not root.exists():
            return {}
        if git_root is None and not (root / ".git").exists():
            from .version_control import LocalGitRepository
            status = LocalGitRepository(root).initialize()
            if not status.initialized:
                raise RuntimeError(status.last_error)
        repo = git_root or root
        prefix = root.relative_to(repo).as_posix() + "/" if repo != root else ""
        paths = self._git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
        files = {}
        for path in paths:
            if not path or (prefix and not path.startswith(prefix)):
                continue
            relative = path[len(prefix):]
            if not source_path(relative):
                continue
            # Generated PDFs have a matching TeX source; imported/reference PDFs remain sources.
            if relative.endswith(".pdf") and (root / relative).with_suffix(".tex").exists():
                continue
            try:
                target = self._target(root, relative)
            except RuntimeError:
                if git_root is not None:
                    raise
                continue
            if target.is_file():
                files[relative] = target.read_bytes()
        return files

    def _prepare(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if not self.repository.exists():
            self._git(self.state_dir, "clone", "--branch", "main", "--", self.remote, str(self.repository))
        actual = self._git(self.repository, "remote", "get-url", "origin").strip()
        if actual != self.remote:
            raise RuntimeError("Project sync cache belongs to another remote")
        if self._git(self.repository, "status", "--porcelain").strip():
            raise RuntimeError("Project sync cache has uncommitted changes; inspect it before retrying")
        self._git(self.repository, "fetch", "origin", "main")
        self._git(self.repository, "merge", "--ff-only", "origin/main")

    def sync_once(self) -> None:
        if not self.remote:
            return
        with self._lock, self._process_lock() as acquired:
            if not acquired:
                return
            try:
                self.discover()
                self._prepare()
                shared = self.repository / "projects"
                if shared.is_dir():
                    for directory in sorted(shared.iterdir()):
                        if directory.is_dir() and not directory.is_symlink() and not directory.name.startswith("."):
                            if directory.name not in self.projects:
                                self.register(self.discovery_root / directory.name)
                actions = []
                baselines = {}
                conflicts = []
                for name, project in self.projects.items():
                    local_root = Path(project["root"])
                    if not local_root.exists() and project["baseline"]:
                        raise RuntimeError(f"Local project is unavailable: {local_root}")
                    remote_root = shared / name
                    local = self._files(local_root)
                    remote = self._files(remote_root, git_root=self.repository)
                    baseline = project["baseline"]
                    for path in baseline:
                        if path not in local and source_path(path):
                            target = self._target(local_root, path)
                            if target.is_file():
                                local[path] = target.read_bytes()
                    next_baseline = {}
                    for path in sorted(local.keys() | remote.keys() | baseline.keys()):
                        left, right = local.get(path), remote.get(path)
                        previous = baseline.get(path)
                        if left == right:
                            resolved = left
                        elif digest(right) == previous:
                            resolved = left
                            actions.append((remote_root, path, right, left))
                        elif digest(left) == previous:
                            resolved = right
                            actions.append((local_root, path, left, right))
                        else:
                            conflicts.append(f"{name}/{path}")
                            continue
                        if resolved is not None:
                            next_baseline[path] = digest(resolved)
                    baselines[name] = next_baseline
                if conflicts:
                    raise RuntimeError("Sync conflict; local and cloud versions preserved: " + ", ".join(conflicts[:10]))
                # Validate all destinations before changing either side.
                for root, path, before, after in actions:
                    target = self._target(root, path)
                    current = target.read_bytes() if target.is_file() else None
                    if current != before:
                        raise RuntimeError(f"File changed during sync; retrying next cycle: {path}")
                for root, path, before, after in actions:
                    target = self._target(root, path)
                    current = target.read_bytes() if target.is_file() else None
                    if current != before:
                        raise RuntimeError(f"File changed during sync: {path}")
                    if after is None:
                        target.unlink(missing_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        # Recheck immediately before replacement, including editor saves during preflight.
                        current = target.read_bytes() if target.is_file() else None
                        if current != before:
                            raise RuntimeError(f"File changed during sync: {path}")
                        fd, temporary = tempfile.mkstemp(prefix=".cloverleaf-sync-", dir=target.parent)
                        try:
                            with os.fdopen(fd, "wb") as handle:
                                handle.write(after)
                            # Preserve executable source scripts in both directions.
                            if root.is_relative_to(shared):
                                source = Path(self.projects[root.name]["root"]) / path
                            else:
                                source = shared / root.name / path
                            os.chmod(temporary, source.stat().st_mode & 0o777)
                            os.replace(temporary, target)
                        finally:
                            Path(temporary).unlink(missing_ok=True)
                if shared.exists():
                    # Source selection already respects local ignores and credential exclusions.
                    # Preserve explicitly tracked sources even when their project's ignore rules match.
                    self._git(self.repository, "add", "--force", "-A", "--", "projects")
                if self._git(self.repository, "diff", "--cached", "--name-only").strip():
                    self._git(self.repository, "-c", "user.name=Cloverleaf", "-c", "user.email=cloverleaf@localhost",
                              "-c", "commit.gpgsign=false", "commit", "--no-verify", "-m", "Sync Cloverleaf projects")
                self._git(self.repository, "push", "origin", "HEAD:main")
                for name, baseline in baselines.items():
                    self.projects[name]["baseline"] = baseline
                self._save()
                self.last_error = None
                self.last_synced = datetime.now(timezone.utc).isoformat()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                self.last_error = str(exc)
                logger.warning("Project sync failed: %s", exc)

    async def run(self) -> None:
        while not self._stop.is_set():
            await asyncio.to_thread(self.sync_once)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
