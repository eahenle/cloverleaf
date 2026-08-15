import os
import shutil
from pathlib import Path, PurePosixPath

from .models import TreeNode


IGNORED_SUFFIXES = {
    ".aux", ".bbl", ".blg", ".fdb_latexmk", ".fls", ".log", ".out",
    ".pdf", ".synctex.gz", ".toc",
}


class WorkspaceError(ValueError):
    pass


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, raw_path: str, *, must_exist: bool = False) -> Path:
        normalized = raw_path.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if not raw_path or pure.is_absolute() or ".." in pure.parts:
            raise WorkspaceError("Path must be a relative path inside the workspace")
        target = (self.root / Path(*pure.parts)).resolve(strict=False)
        if not target.is_relative_to(self.root):
            raise WorkspaceError("Path escapes the workspace")
        # Existing symlink ancestors are covered by resolve(); reject broken links too.
        cursor = self.root
        for part in pure.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                resolved = cursor.resolve(strict=False)
                if not resolved.is_relative_to(self.root):
                    raise WorkspaceError("Symlink escapes the workspace")
        if must_exist and not target.exists():
            raise FileNotFoundError(raw_path)
        return target

    @staticmethod
    def is_hidden_build_file(path: Path) -> bool:
        name = path.name
        return name.startswith(".") or any(name.endswith(s) for s in IGNORED_SUFFIXES)

    def tree(self) -> list[TreeNode]:
        def visit(directory: Path) -> list[TreeNode]:
            nodes: list[TreeNode] = []
            for item in sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
                if self.is_hidden_build_file(item) or item.is_symlink():
                    continue
                rel = item.relative_to(self.root).as_posix()
                if item.is_dir():
                    nodes.append(TreeNode(name=item.name, path=rel, type="directory", children=visit(item)))
                elif item.is_file():
                    nodes.append(TreeNode(name=item.name, path=rel, type="file"))
            return nodes

        return visit(self.root)

    def read(self, path: str) -> str:
        target = self.resolve(path, must_exist=True)
        if not target.is_file():
            raise IsADirectoryError(path)
        return target.read_text(encoding="utf-8")

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
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def rename(self, path: str, new_path: str) -> None:
        source = self.resolve(path, must_exist=True)
        target = self.resolve(new_path)
        if target.exists():
            raise FileExistsError(new_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)

    def delete(self, path: str) -> None:
        target = self.resolve(path, must_exist=True)
        if target == self.root:
            raise WorkspaceError("Cannot delete workspace root")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            os.unlink(target)
