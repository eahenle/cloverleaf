from __future__ import annotations

import os
import re
import shutil
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from .models import TreeNode


IGNORED_SUFFIXES = {
    ".aux",
    ".bbl",
    ".blg",
    ".fdb_latexmk",
    ".fls",
    ".log",
    ".out",
    ".pdf",
    ".synctex.gz",
    ".toc",
}
WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:/")


class WorkspaceError(ValueError):
    pass


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _decode(raw_path: str) -> str:
        decoded = raw_path
        for _ in range(3):
            expanded = unquote(decoded)
            if expanded == decoded:
                break
            decoded = expanded
        return decoded

    @staticmethod
    def is_hidden_build_file(path: Path) -> bool:
        name = path.name
        return name.startswith(".") or any(name.endswith(suffix) for suffix in IGNORED_SUFFIXES)

    def resolve(self, raw_path: str, *, must_exist: bool = False) -> Path:
        decoded = self._decode(raw_path)
        normalized = decoded.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if (
            not decoded
            or "\x00" in decoded
            or pure.is_absolute()
            or WINDOWS_ABSOLUTE.match(normalized)
            or any(part in {"", ".", ".."} or part.startswith(".") for part in pure.parts)
        ):
            raise WorkspaceError("Path must be a visible relative path inside the workspace")
        if any(pure.name.endswith(suffix) for suffix in IGNORED_SUFFIXES):
            raise WorkspaceError("LaTeX build artifacts are not available through the file API")

        target = self.root.joinpath(*pure.parts)
        cursor = self.root
        for part in pure.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise WorkspaceError("Symbolic links are not available through the file API")

        resolved = target.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise WorkspaceError("Path escapes the workspace")
        if must_exist and not target.exists():
            raise FileNotFoundError(raw_path)
        return target

    def tree(self) -> list[TreeNode]:
        def visit(directory: Path) -> list[TreeNode]:
            nodes: list[TreeNode] = []
            for item in sorted(directory.iterdir(), key=lambda path: (path.is_file(), path.name.lower())):
                if self.is_hidden_build_file(item) or item.is_symlink():
                    continue
                relative = item.relative_to(self.root).as_posix()
                if item.is_dir():
                    nodes.append(
                        TreeNode(
                            name=item.name,
                            path=relative,
                            type="directory",
                            children=visit(item),
                        )
                    )
                elif item.is_file():
                    nodes.append(TreeNode(name=item.name, path=relative, type="file"))
            return nodes

        return visit(self.root)

    def read(self, path: str) -> str:
        target = self.resolve(path, must_exist=True)
        if not target.is_file():
            raise IsADirectoryError(path)
        try:
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError("Only UTF-8 text files can be opened") from exc

    def write(self, path: str, content: str) -> None:
        target = self.resolve(path)
        if target.exists() and target.is_dir():
            raise IsADirectoryError(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def create(self, path: str, entry_type: str, content: str = "") -> None:
        target = self.resolve(path)
        if target.exists():
            raise FileExistsError(path)
        if entry_type == "directory":
            target.mkdir(parents=True)
        elif entry_type == "file":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        else:
            raise WorkspaceError("Entry type must be file or directory")

    def rename(self, path: str, new_path: str) -> None:
        source = self.resolve(path, must_exist=True)
        target = self.resolve(new_path)
        if target.exists():
            raise FileExistsError(new_path)
        if source.is_dir() and target.is_relative_to(source):
            raise WorkspaceError("Cannot move a directory inside itself")
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)

    def delete(self, path: str) -> None:
        target = self.resolve(path, must_exist=True)
        if target.is_dir():
            shutil.rmtree(target)
        else:
            os.unlink(target)
