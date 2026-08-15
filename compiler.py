import asyncio
import re
import shutil
from pathlib import Path

from .models import CompileStatus, Diagnostic


LATEX_ERROR = re.compile(r"^!\s*(.+)$", re.MULTILINE)
FILE_LINE_ERROR = re.compile(r"^(.+?\.tex):(\d+):\s*(.+)$", re.MULTILINE)
WARNING = re.compile(r"^(?:LaTeX|Package .+?) Warning:\s*(.+?)(?:\s+on input line (\d+))?\.$", re.MULTILINE)


def parse_diagnostics(output: str) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    seen: set[tuple[str, str | None, int | None]] = set()

    for match in FILE_LINE_ERROR.finditer(output):
        key = (match.group(3).strip(), match.group(1), int(match.group(2)))
        if key not in seen:
            diagnostics.append(Diagnostic(severity="error", message=key[0], file=key[1], line=key[2]))
            seen.add(key)
    for match in LATEX_ERROR.finditer(output):
        message = match.group(1).strip()
        key = (message, None, None)
        if key not in seen:
            diagnostics.append(Diagnostic(severity="error", message=message))
            seen.add(key)
    for match in WARNING.finditer(output):
        message = match.group(1).strip()
        line = int(match.group(2)) if match.group(2) else None
        key = (message, None, line)
        if key not in seen:
            diagnostics.append(Diagnostic(severity="warning", message=message, line=line))
            seen.add(key)
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
            self._task = asyncio.create_task(self._run_requested())
        return self.status

    async def wait(self) -> CompileStatus:
        self.request()
        if self._task:
            await self._task
        return self.status

    async def _run_requested(self) -> None:
        while self._pending:
            self._pending = False
            await self._compile()

    async def _compile(self) -> None:
        async with self._lock:
            revision = self.status.revision
            self.status = CompileStatus(state="compiling", revision=revision)
            executable = shutil.which("latexmk")
            if not executable:
                self.status = CompileStatus(
                    state="error",
                    diagnostics=[Diagnostic(severity="error", message="latexmk is not installed or not on PATH")],
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
                if not success and not any(d.severity == "error" for d in diagnostics):
                    diagnostics.insert(0, Diagnostic(severity="error", message="LaTeX compilation failed; inspect the log excerpt below"))
                self.status = CompileStatus(
                    state="success" if success else "error",
                    diagnostics=diagnostics,
                    log_tail="\n".join(output.splitlines()[-80:]),
                    revision=revision + (1 if success else 0),
                )
            except OSError as exc:
                self.status = CompileStatus(
                    state="error",
                    diagnostics=[Diagnostic(severity="error", message=f"Could not run latexmk: {exc}")],
                    revision=revision,
                )

    @property
    def pdf_path(self) -> Path:
        return self.workspace / Path(self.main_file).with_suffix(".pdf")
