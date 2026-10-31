import asyncio
from pathlib import Path

import pytest

from cloverleaf.compiler import Compiler, find_latexmk, parse_diagnostics


def test_parse_diagnostics_with_file_lines_and_warnings() -> None:
    output = """./sections/introduction.tex:7: Undefined control sequence.
! Missing } inserted.
l.12 \\section{Broken
LaTeX Warning: Label `missing' multiply defined on input line 18.
"""

    diagnostics = parse_diagnostics(output)

    assert diagnostics[0].model_dump() == {
        "severity": "error",
        "message": "Undefined control sequence.",
        "file": "sections/introduction.tex",
        "line": 7,
    }
    assert diagnostics[1].line == 12
    assert "Missing } inserted" in diagnostics[1].message
    assert diagnostics[2].severity == "warning"
    assert diagnostics[2].line == 18


def test_nested_compilation_root_keeps_outputs_beside_source(tmp_path: Path) -> None:
    root_compiler = Compiler(tmp_path, "main.tex")
    nested_compiler = Compiler(tmp_path, "opportunities/nvidia-jr2015929/resume.tex")

    assert not any(
        argument.startswith("-output-directory=")
        for argument in root_compiler.latexmk_arguments
    )
    assert nested_compiler.latexmk_arguments[-2:] == (
        "-output-directory=opportunities/nvidia-jr2015929",
        "opportunities/nvidia-jr2015929/resume.tex",
    )
    assert nested_compiler.pdf_path == (
        tmp_path / "opportunities/nvidia-jr2015929/resume.pdf"
    )


@pytest.mark.asyncio
async def test_nested_compilation_writes_no_root_artifacts(tmp_path: Path) -> None:
    if find_latexmk() is None:
        pytest.skip("latexmk is unavailable")
    opportunity = tmp_path / "opportunities" / "example-role"
    opportunity.mkdir(parents=True)
    source = opportunity / "resume.tex"
    source.write_text(
        "\\documentclass{article}\\begin{document}Nested output\\end{document}\n",
        encoding="utf-8",
    )

    status = await Compiler(tmp_path, "opportunities/example-role/resume.tex").wait()

    assert status.state == "success"
    assert (opportunity / "resume.pdf").is_file()
    assert not (tmp_path / "resume.pdf").exists()
    assert not (tmp_path / "resume.aux").exists()


@pytest.mark.asyncio
async def test_missing_latexmk_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compiler = Compiler(tmp_path, "main.tex")
    monkeypatch.setattr("cloverleaf.compiler.shutil.which", lambda _name: None)
    monkeypatch.setattr("cloverleaf.compiler.LATEXMK_LOCATIONS", ())

    status = await compiler.wait()

    assert status.state == "error"
    assert status.revision == 0
    assert status.diagnostics[0].message == "latexmk is not installed or is not on PATH"


@pytest.mark.asyncio
async def test_requests_during_compile_are_queued_without_overlap(tmp_path: Path) -> None:
    compiler = Compiler(tmp_path, "main.tex")
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    active = 0
    max_active = 0

    async def fake_compile() -> None:
        nonlocal calls, active, max_active
        calls += 1
        active += 1
        max_active = max(max_active, active)
        if calls == 1:
            started.set()
            await release.wait()
        await asyncio.sleep(0)
        active -= 1

    compiler._compile = fake_compile  # type: ignore[method-assign]
    compiler.request()
    await started.wait()
    compiler.request()
    release.set()
    assert compiler._task is not None
    await compiler._task

    assert calls == 2
    assert max_active == 1


@pytest.mark.asyncio
async def test_synctex_maps_pdf_coordinates_to_an_included_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil
    import subprocess

    synctex = shutil.which("synctex")
    if not find_latexmk() or not synctex:
        pytest.skip("TeX tools unavailable")
    nested = tmp_path / "paper"
    nested.mkdir()
    included = nested / "body.tex"
    included.write_text("First paragraph.\n\nA distinctive source sentence.\n", encoding="utf-8")
    (nested / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\input{body}\n\\end{document}\n",
        encoding="utf-8",
    )
    compiler = Compiler(tmp_path, "paper/main.tex")
    assert (await compiler.wait()).state == "success"
    assert (nested / "main.synctex.gz").exists()
    (nested / "main.synctex.gz").unlink()
    assert (await compiler.wait()).state == "success"
    assert (nested / "main.synctex.gz").exists()
    output = subprocess.run(
        [synctex, "view", "-i", f"3:0:{included}", "-o", str(compiler.pdf_path)],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout
    values = dict(line.split(":", 1) for line in output.splitlines() if ":" in line)
    marker = tmp_path / "external-editor-was-launched"
    monkeypatch.setenv("SYNCTEX_EDITOR", f"touch {marker}")
    location = await compiler.source_location(int(values["Page"]), float(values["x"]), float(values["y"]))
    assert Path(location[0]).resolve() == included.resolve()
    assert abs(location[1] - 3) <= 1
    assert not marker.exists()


@pytest.mark.asyncio
async def test_source_lookup_reports_missing_synctex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cloverleaf.compiler.shutil.which", lambda _name: None)
    monkeypatch.setattr("cloverleaf.compiler.find_latexmk", lambda: None)
    with pytest.raises(FileNotFoundError, match="SyncTeX is unavailable"):
        await Compiler(tmp_path, "main.tex").source_location(1, 100, 200)
