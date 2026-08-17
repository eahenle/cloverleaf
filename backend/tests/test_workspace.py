from pathlib import Path

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
    assert workspace.tree() == []


def test_tree_hides_latex_artifacts(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "manuscript")
    (workspace.root / "main.tex").write_text("source", encoding="utf-8")
    (workspace.root / "main.pdf").write_bytes(b"pdf")
    (workspace.root / "main.aux").write_text("aux", encoding="utf-8")

    assert [node.name for node in workspace.tree()] == ["main.tex"]
