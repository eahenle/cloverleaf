import hashlib
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from cloverleaf.workspace import FileChangedError, Workspace, WorkspaceError


def test_file_lifecycle_and_tree(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "manuscript")

    workspace.create("sections", "directory")
    workspace.create("sections/draft.tex", "file", "first")
    assert workspace.read("sections/draft.tex") == "first"

    workspace.write("sections/draft.tex", "second")
    workspace.rename("sections/draft.tex", "sections/introduction.tex")
    assert workspace.read("sections/introduction.tex") == "second"
    assert workspace.tree()[0].children[0].path == "sections/introduction.tex"

    workspace.delete("sections/introduction.tex")
    workspace.delete("sections")
    assert workspace.tree() == []


def test_versioned_write_rejects_external_changes(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "manuscript")
    workspace.create("main.tex", "file", "first")
    _content, version = workspace.read_versioned("main.tex")
    (workspace.root / "main.tex").write_text("external", encoding="utf-8")

    with pytest.raises(FileChangedError, match="changed on disk"):
        workspace.write("main.tex", "stale editor", expected_version=version)

    assert workspace.read("main.tex") == "external"


@pytest.mark.parametrize("raw", [b"", b"plain text", "é🙂\r\n第二\rline\n".encode("utf-8")])
def test_bounded_versioned_read_accepts_exact_byte_limit_with_same_version(
    tmp_path: Path, raw: bytes,
) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / "main.tex").write_bytes(raw)
    expected = workspace.read_versioned("main.tex")

    assert workspace.read_versioned("main.tex", max_bytes=len(raw)) == expected


def test_bounded_versioned_read_rejects_large_file_before_opening(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = Workspace(tmp_path)
    target = tmp_path / "large.txt"
    target.write_text("é" * 8, encoding="utf-8")

    def unexpected_open(*args, **kwargs):
        pytest.fail("Oversized files must be rejected before opening their contents")

    monkeypatch.setattr(Path, "open", unexpected_open)
    with pytest.raises(WorkspaceError, match=r"large\.txt.*8.byte limit"):
        workspace.read_versioned("large.txt", max_bytes=8)


def test_bounded_versioned_read_limits_growth_after_size_check(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = Workspace(tmp_path)
    target = tmp_path / "growing.txt"
    target.write_bytes(b"tiny")
    original_open = Path.open
    reads = []

    @contextmanager
    def growing_open(path, *args, **kwargs):
        with original_open(path, *args, **kwargs) as source:
            def read(size=-1):
                # Grow the real file after the reader's stat and open checks.
                with original_open(target, "ab") as writer:
                    writer.write(b"x" * 1_000_000)
                result = source.read(size)
                reads.append((size, len(result)))
                return result

            yield SimpleNamespace(read=read)

    def unexpected_hash(*args, **kwargs):
        pytest.fail("Oversized contents must be rejected before hashing")

    monkeypatch.setattr(Path, "open", growing_open)
    monkeypatch.setattr(hashlib, "sha256", unexpected_hash)
    with pytest.raises(WorkspaceError, match=r"growing\.txt.*8.byte limit"):
        workspace.read_versioned("growing.txt", max_bytes=8)
    assert reads == [(9, 9)]


def test_bounded_versioned_read_rejects_invalid_utf8(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / "binary.txt").write_bytes(b"\xff")

    with pytest.raises(WorkspaceError, match="Only UTF-8"):
        workspace.read_versioned("binary.txt", max_bytes=1)


def test_bounded_versioned_read_rejects_negative_limit(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    workspace.create("main.tex", "file", "manuscript")

    with pytest.raises(ValueError, match="non-negative"):
        workspace.read_versioned("main.tex", max_bytes=-1)


@pytest.mark.parametrize("keep_changing", [False, True])
def test_bounded_versioned_read_retries_changes_without_returning_partial_version(
    tmp_path: Path, monkeypatch, keep_changing: bool,
) -> None:
    workspace = Workspace(tmp_path)
    target = tmp_path / "main.tex"
    target.write_bytes(b"old")
    original_open = Path.open
    reads = []

    @contextmanager
    def changing_open(path, *args, **kwargs):
        with original_open(path, *args, **kwargs) as source:
            def read(size=-1):
                result = source.read(size)
                reads.append(size)
                if keep_changing or len(reads) == 1:
                    previous = target.stat()
                    with original_open(target, "wb") as writer:
                        writer.write(b"new")
                    os.utime(target, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1))
                return result

            yield SimpleNamespace(read=read)

    monkeypatch.setattr(Path, "open", changing_open)
    if keep_changing:
        with pytest.raises(FileChangedError, match="changed while Cloverleaf was reading"):
            workspace.read_versioned("main.tex", max_bytes=3)
    else:
        assert workspace.read_versioned("main.tex", max_bytes=3) == (
            "new", hashlib.sha256(b"new").hexdigest()
        )
    assert reads == [4, 4]


def test_reviewed_edits_apply_together_after_all_versions_validate(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "manuscript")
    workspace.create("main.tex", "file", "main original")
    workspace.create("sections/results.tex", "file", "results original")
    _main, main_version = workspace.read_versioned("main.tex")
    _results, results_version = workspace.read_versioned("sections/results.tex")

    applied = workspace.apply_edits(
        [
            ("main.tex", "main revised", main_version, False),
            ("sections/results.tex", "results revised", results_version, False),
            ("sections/new.tex", "new section", None, True),
        ]
    )

    assert [path for path, _content, _version in applied] == [
        "main.tex",
        "sections/results.tex",
        "sections/new.tex",
    ]
    assert workspace.read("main.tex") == "main revised"
    assert workspace.read("sections/results.tex") == "results revised"
    assert workspace.read("sections/new.tex") == "new section"


def test_reviewed_edit_set_changes_nothing_when_one_version_is_stale(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "manuscript")
    workspace.create("main.tex", "file", "main original")
    workspace.create("results.tex", "file", "results original")
    _main, main_version = workspace.read_versioned("main.tex")
    _results, stale_results_version = workspace.read_versioned("results.tex")
    (workspace.root / "results.tex").write_text("human revision", encoding="utf-8")

    with pytest.raises(FileChangedError, match="did not apply any"):
        workspace.apply_edits(
            [
                ("main.tex", "Codex main", main_version, False),
                ("results.tex", "Codex results", stale_results_version, False),
            ]
        )

    assert workspace.read("main.tex") == "main original"
    assert workspace.read("results.tex") == "human revision"


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../secret.tex",
        "%2e%2e/secret.tex",
        "%252e%252e/secret.tex",
        "..\\secret.tex",
        "C:\\Windows\\system.ini",
        ".env",
        "main.pdf",
    ],
)
def test_rejects_unsafe_paths(tmp_path: Path, path: str) -> None:
    workspace = Workspace(tmp_path / "manuscript")
    with pytest.raises(WorkspaceError):
        workspace.resolve(path)
    with pytest.raises(WorkspaceError):
        workspace.read_versioned(path, max_bytes=8)


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace_root = tmp_path / "manuscript"
    workspace_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.tex").write_text("secret", encoding="utf-8")
    try:
        (workspace_root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks are unavailable on this platform")

    workspace = Workspace(workspace_root)
    with pytest.raises(WorkspaceError, match="Symbolic links"):
        workspace.read("linked/secret.tex")
    with pytest.raises(WorkspaceError, match="Symbolic links"):
        workspace.read_versioned("linked/secret.tex", max_bytes=8)
    assert workspace.tree() == []


def test_tree_hides_latex_artifacts(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "manuscript")
    (workspace.root / "main.tex").write_text("source", encoding="utf-8")
    (workspace.root / "main.pdf").write_bytes(b"pdf")
    (workspace.root / "main.aux").write_text("aux", encoding="utf-8")

    assert [node.name for node in workspace.tree()] == ["main.tex"]
