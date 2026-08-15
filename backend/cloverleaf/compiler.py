from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from .models import CompileStatus, Diagnostic


FILE_LINE_ERROR = re.compile(r"^(?:\./)?(.+?\.tex):(\d+):\s*(.+)$")
LATEX_ERROR = re.compile(r"^!\s*(.+)$")
CONTEXT_LINE = re.compile(r"^l\.(\d+)\s*(.*)$")
WARNING = re.compile(
    r"^(?:LaTeX|Package .+?) Warning:\s*(.+?)(?:\s+on input line (\d+))?\.?$"
)
LATEXMK_LOCATIONS = (
    Path("/Library/TeX/texbin/latexmk"),
    Path("/opt/homebrew/opt/texlive/bin/latexmk"),
    Path("/usr/local/opt/texlive/bin/latexmk"),
)


def find_latexmk() -> str | None:
    executable = shutil.which("latexmk")
    if executable:
        return executable
    return next((str(path) for path in LATEXMK_LOCATIONS if path.is_file()), None)


def parse_diagnostics(output: str) -> list[Diagnostic]:
    lines = output.splitlines()
    diagnostics: list[Diagnostic] = []
    seen: set[tuple[str, str | None, int | None]] = set()

    def append(diagnostic: Diagnostic) -> None:
        key = (diagnostic.message, diagnostic.file, diagnostic.line)
        if key not in seen:
            diagnostics.append(diagnostic)
            seen.add(key)

    for index, line in enumerate(lines):
        file_match = FILE_LINE_ERROR.match(line)
        if file_match:
            append(
                Diagnostic(
                    severity="error",
                    message=file_match.group(3).strip(),
                    file=file_match.group(1),
                    line=int(file_match.group(2)),
                )
            )
            continue

        error_match = LATEX_ERROR.match(line)
        if error_match:
            line_number = None
            detail = ""
            for candidate in lines[index + 1 : index + 5]:
                context_match = CONTEXT_LINE.match(candidate)
                if context_match:
                    line_number = int(context_match.group(1))
                    detail = context_match.group(2).strip()
                    break
            message = error_match.group(1).strip()
            if detail and detail not in message:
                message = f"{message} ({detail})"
            append(Diagnostic(severity="error", message=message, line=line_number))
            continue

        warning_match = WARNING.match(line)
        if warning_match:
            append(
                Diagnostic(
                    severity="warning",
                    message=warning_match.group(1).strip(),
                    line=int(warning_match.group(2)) if warning_match.group(2) else None,
                )
            )

    return diagnostics[:50]


class Compiler:
    def __init__(self, workspace: Path, main_file: str):
        self.workspace = workspace
        self.main_file = main_file
        self.status = CompileStatus()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._pending = False

    def request(self) -> CompileStatus:
        self._pending = True
        if self._task is None or self._task.done():
            self.status = CompileStatus(state="compiling", revision=self.status.revision)
            self._task = asyncio.create_task(self._run_requested())
        return self.status.model_copy(deep=True)

    async def wait(self) -> CompileStatus:
        self.request()
        if self._task:
            await self._task
        return self.status.model_copy(deep=True)

    async def _run_requested(self) -> None:
        while self._pending:
            self._pending = False
            await self._compile()

    async def _compile(self) -> None:
        async with self._lock:
            revision = self.status.revision
            self.status = CompileStatus(state="compiling", revision=revision)
            executable = find_latexmk()
            if not executable:
                self.status = CompileStatus(
                    state="error",
                    diagnostics=[
                        Diagnostic(
                            severity="error",
                            message="latexmk is not installed or is not on PATH",
                        )
                    ],
                    revision=revision,
                )
                return

            try:
                process = await asyncio.create_subprocess_exec(
                    executable,
                    "-pdf",
                    "-interaction=nonstopmode",
                    "-halt-on-error",
                    "-file-line-error",
                    self.main_file,
                    cwd=self.workspace,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await process.communicate()
                output = stdout.decode("utf-8", errors="replace")
                diagnostics = parse_diagnostics(output)
                success = process.returncode == 0 and self.pdf_path.exists()
                if not success and not any(item.severity == "error" for item in diagnostics):
                    diagnostics.insert(
                        0,
                        Diagnostic(
                            severity="error",
                            message="LaTeX compilation failed; expand the build log for details",
                        ),
                    )
                self.status = CompileStatus(
                    state="success" if success else "error",
                    diagnostics=diagnostics,
                    log_tail="\n".join(output.splitlines()[-80:]),
                    revision=revision + (1 if success else 0),
                )
            except OSError as exc:
                self.status = CompileStatus(
                    state="error",
                    diagnostics=[
                        Diagnostic(severity="error", message=f"Could not run latexmk: {exc}")
                    ],
                    revision=revision,
                )

    @property
    def pdf_path(self) -> Path:
        return self.workspace / Path(self.main_file).with_suffix(".pdf")
