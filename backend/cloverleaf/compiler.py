from __future__ import annotations

import asyncio
import os
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

    async def finish(self) -> None:
        """Wait for already-requested builds without scheduling another one."""
        task = self._task
        if task and not task.done():
            await task

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
                    *self.latexmk_arguments,
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

    async def source_location(self, page: int, x: float, y: float) -> tuple[str, int, int]:
        async with self._lock:
            return await self._source_location(page, x, y)

    async def _source_location(self, page: int, x: float, y: float) -> tuple[str, int, int]:
        executable = shutil.which("synctex")
        if executable is None:
            latexmk = find_latexmk()
            sibling = Path(latexmk).with_name("synctex") if latexmk else None
            executable = str(sibling) if sibling and sibling.is_file() else None
        if executable is None:
            raise FileNotFoundError("SyncTeX is unavailable; install the TeX synctex utility")
        if not self.pdf_path.with_suffix(".synctex.gz").exists() and not self.pdf_path.with_suffix(".synctex").exists():
            raise FileNotFoundError("Source mapping is unavailable; recompile the manuscript first")
        environment = os.environ.copy()
        environment.pop("SYNCTEX_EDITOR", None)
        process = await asyncio.create_subprocess_exec(
            executable, "edit", "-o", f"{page}:{x}:{y}:{self.pdf_path}",
            cwd=self.workspace, env=environment,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        result: dict[str, str] = {}
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            if line == "SyncTeX result begin":
                result = {}
            elif line == "SyncTeX result end":
                try:
                    source_line = int(result["Line"])
                    column = max(0, int(result.get("Column", "0")))
                    if source_line > 0 and result.get("Input"):
                        return result["Input"], source_line, column
                except (ValueError, KeyError):
                    pass
            elif ":" in line:
                key, value = line.split(":", 1)
                result[key] = value
        raise ValueError("No source location was found near this point")

    @property
    def pdf_path(self) -> Path:
        return self.workspace / Path(self.main_file).with_suffix(".pdf")

    @property
    def latexmk_arguments(self) -> tuple[str, ...]:
        arguments = [
            "-pdf",
            "-synctex=1",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-file-line-error",
        ]
        if (self.pdf_path.exists()
                and not self.pdf_path.with_suffix(".synctex.gz").exists()
                and not self.pdf_path.with_suffix(".synctex").exists()):
            arguments.append("-g")
        output_directory = Path(self.main_file).parent
        if output_directory != Path("."):
            arguments.append(f"-output-directory={output_directory.as_posix()}")
        arguments.append(self.main_file)
        return tuple(arguments)
