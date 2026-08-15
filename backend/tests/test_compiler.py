import asyncio
from pathlib import Path

import pytest

from cloverleaf.compiler import Compiler, parse_diagnostics


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
