from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import Depends, FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .assistant import (
    AssistantProvider,
    AssistantUnavailable,
    CodexProvider,
    OpenAICompatibleProvider,
    UnconfiguredProvider,
)
from .compiler import Compiler
from .config import Settings, get_settings
from .models import (
    ApplyEditsRequest,
    ApplyEditsResponse,
    AssistantRequest,
    AssistantProgress,
    AssistantResponse,
    CreateEntry,
    DirectoryEntry,
    DirectoryListing,
    FileContent,
    LoadProject,
    ProjectInfo,
    ProposedEdit,
    RenameEntry,
)
from .workspace import FileChangedError, Workspace, WorkspaceError


logger = logging.getLogger(__name__)
ASSISTANT_HEARTBEAT_SECONDS = 3

DEFAULT_MAIN_TEX = """\\documentclass{article}
\\title{Untitled manuscript}
\\author{}
\\date{}

\\begin{document}
\\maketitle

Start writing here.

\\end{document}
"""


def restore_project(settings: Settings) -> tuple[Path, str]:
    state_path = settings.project_state_path
    if not state_path.exists():
        return settings.workspace, settings.main_file
    try:
        saved = LoadProject.model_validate_json(state_path.read_text(encoding="utf-8"))
        candidate = Path(saved.workspace).expanduser()
        if not candidate.is_absolute() or not candidate.exists() or not candidate.is_dir():
            raise ValueError("saved workspace is unavailable")
        candidate = candidate.resolve()
        saved_workspace = Workspace(candidate)
        main_path = saved_workspace.resolve(saved.main_file, must_exist=True)
        if not main_path.is_file() or main_path.suffix.lower() != ".tex":
            raise ValueError("saved compilation root is unavailable")
        return candidate, main_path.relative_to(candidate).as_posix()
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Ignoring invalid saved project selection at %s: %s", state_path, exc)
        return settings.workspace, settings.main_file


def persist_project(state_path: Path, workspace: Path, main_file: str) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_name(f"{state_path.name}.tmp")
    temporary.write_text(
        json.dumps({"workspace": str(workspace), "main_file": main_file}, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(state_path)


def build_assistant(settings: Settings, workspace: Path | None = None) -> AssistantProvider:
    if settings.ai_provider == "codex":
        return CodexProvider(
            settings.ai_model,
            str(workspace or settings.workspace),
            settings.codex_bin or None,
        )
    if (
        settings.ai_provider == "openai-compatible"
        and settings.ai_api_key
        and settings.ai_base_url
        and settings.ai_model
    ):
        return OpenAICompatibleProvider(
            settings.ai_base_url, settings.ai_api_key, settings.ai_model
        )
    return UnconfiguredProvider()


def create_app(
    configured_settings: Settings | None = None,
    configured_assistant: AssistantProvider | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = configured_settings or get_settings()
        initial_workspace, initial_main_file = restore_project(settings)
        app.state.workspace = Workspace(initial_workspace)
        app.state.compiler = Compiler(initial_workspace, initial_main_file)
        if configured_assistant is None:
            app.state.assistant = build_assistant(settings, initial_workspace)
        elif isinstance(configured_assistant, CodexProvider):
            app.state.assistant = configured_assistant.for_workspace(initial_workspace)
        else:
            app.state.assistant = configured_assistant
        app.state.settings = settings
        app.state.main_file = initial_main_file
        app.state.project_state_path = settings.project_state_path
        app.state.project_lock = asyncio.Lock()
        app.state.assistant_progress = {}
        yield

    application = FastAPI(title="Cloverleaf", version="0.1.0", lifespan=lifespan)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )

    def workspace() -> Workspace:
        return application.state.workspace

    @application.get("/api/health")
    async def health():
        return {"ok": True}

    @application.get("/api/runtime")
    async def runtime_info():
        log_path = application.state.settings.runtime_log_path
        shutdown_path = application.state.settings.runtime_shutdown_path
        return {
            "managed": log_path is not None and shutdown_path is not None,
            "log_available": log_path is not None and log_path.is_file(),
            "shutdown_available": shutdown_path is not None,
        }

    @application.websocket("/api/runtime/logs")
    async def runtime_logs(socket: WebSocket):
        log_path = application.state.settings.runtime_log_path
        if log_path is None or not log_path.is_file():
            await socket.close(code=1008, reason="Runtime logs are unavailable")
            return

        await socket.accept()
        position = max(0, log_path.stat().st_size - 200_000)
        inode: int | None = None
        if position:
            await socket.send_text("[... earlier server output omitted ...]\n")
        try:
            while True:
                try:
                    stat = log_path.stat()
                    if inode is not None and stat.st_ino != inode:
                        position = 0
                    inode = stat.st_ino
                    if stat.st_size < position:
                        position = 0
                    if stat.st_size > position:
                        with log_path.open("rb") as handle:
                            handle.seek(position)
                            chunk = handle.read(64_000)
                        position += len(chunk)
                        await socket.send_text(chunk.decode("utf-8", errors="replace"))
                except FileNotFoundError:
                    position = 0
                    inode = None
                try:
                    await asyncio.wait_for(socket.receive_text(), timeout=0.35)
                except TimeoutError:
                    pass
        except WebSocketDisconnect:
            return

    @application.post("/api/runtime/shutdown", status_code=202)
    async def runtime_shutdown():
        shutdown_path = application.state.settings.runtime_shutdown_path
        if shutdown_path is None:
            raise HTTPException(
                status_code=409,
                detail="Shutdown is available only when Cloverleaf is started by its launcher",
            )
        if not shutdown_path.parent.is_dir():
            raise HTTPException(status_code=503, detail="The Cloverleaf launcher is unavailable")
        try:
            shutdown_path.write_text("shutdown requested\n", encoding="utf-8")
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail="Cloverleaf could not contact its launcher",
            ) from exc
        return {"ok": True, "message": "Server shutdown requested"}

    @application.get("/api/project/tree")
    async def get_tree(fs: Workspace = Depends(workspace)):
        return fs.tree()

    def project_info() -> ProjectInfo:
        root = application.state.workspace.root
        return ProjectInfo(
            workspace=str(root),
            name=root.name or str(root),
            main_file=application.state.main_file,
        )

    @application.get("/api/project", response_model=ProjectInfo)
    async def get_project():
        return project_info()

    @application.get("/api/project/directories", response_model=DirectoryListing)
    async def browse_project_directories(path: str | None = None):
        try:
            candidate = (
                application.state.workspace.root
                if path is None
                else Path(path).expanduser()
            )
            if not candidate.is_absolute():
                raise HTTPException(
                    status_code=400,
                    detail="Folder picker paths must be absolute",
                )
            candidate = candidate.resolve()
            if not candidate.exists():
                raise HTTPException(status_code=404, detail="Directory does not exist")
            if not candidate.is_dir():
                raise HTTPException(status_code=400, detail="Path is not a directory")

            directories: list[DirectoryEntry] = []
            tex_files: list[str] = []
            for entry in sorted(candidate.iterdir(), key=lambda item: item.name.lower()):
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                try:
                    if entry.is_dir():
                        directories.append(
                            DirectoryEntry(name=entry.name, path=str(entry.resolve()))
                        )
                    elif entry.is_file() and entry.suffix.lower() == ".tex":
                        tex_files.append(entry.name)
                except OSError:
                    continue
        except HTTPException:
            raise
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Directory is not readable") from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Directory could not be opened") from exc

        parent = candidate.parent if candidate.parent != candidate else None
        return DirectoryListing(
            path=str(candidate),
            parent=str(parent) if parent else None,
            home=str(Path.home().resolve()),
            root=str(Path(candidate.anchor or "/").resolve()),
            directories=directories,
            tex_files=tex_files,
        )

    @application.post("/api/project/load", response_model=ProjectInfo)
    async def load_project(body: LoadProject):
        raw_workspace = body.workspace.strip()
        candidate = Path(raw_workspace).expanduser()
        if not raw_workspace or not candidate.is_absolute():
            raise HTTPException(
                status_code=400,
                detail="Project directory must be an absolute path",
            )
        candidate = candidate.resolve()
        if not candidate.exists():
            raise HTTPException(status_code=404, detail="Project directory does not exist")
        if not candidate.is_dir():
            raise HTTPException(status_code=400, detail="Project path must be a directory")
        if not body.main_file.endswith(".tex"):
            raise HTTPException(
                status_code=400,
                detail="Compilation root must be a relative .tex path",
            )

        next_workspace = Workspace(candidate)
        create_main = False
        try:
            main_path = next_workspace.resolve(body.main_file, must_exist=True)
        except FileNotFoundError as exc:
            try:
                direct_tex_files = [
                    entry
                    for entry in candidate.iterdir()
                    if not entry.name.startswith(".")
                    and not entry.is_symlink()
                    and entry.is_file()
                    and entry.suffix.lower() == ".tex"
                ]
            except OSError as browse_exc:
                raise HTTPException(
                    status_code=403,
                    detail="Project directory is not readable",
                ) from browse_exc
            if body.main_file == "main.tex" and not direct_tex_files:
                main_path = next_workspace.resolve("main.tex")
                create_main = True
            else:
                raise translate_error(exc) from exc
        except Exception as exc:
            raise translate_error(exc) from exc
        if not create_main and not main_path.is_file():
            raise HTTPException(status_code=400, detail="Compilation root must be a file")
        main_file = main_path.relative_to(candidate).as_posix()

        async with application.state.project_lock:
            await application.state.compiler.finish()
            if create_main and not main_path.exists():
                next_workspace.create("main.tex", "file", DEFAULT_MAIN_TEX)
            elif create_main and not main_path.is_file():
                raise HTTPException(status_code=400, detail="Compilation root must be a file")
            try:
                persist_project(application.state.project_state_path, candidate, main_file)
            except OSError as exc:
                raise HTTPException(
                    status_code=500,
                    detail="The project is valid, but Cloverleaf could not save the selection",
                ) from exc
            application.state.workspace = next_workspace
            application.state.compiler = Compiler(candidate, main_file)
            application.state.main_file = main_file
            if configured_assistant is None:
                application.state.assistant = build_assistant(
                    application.state.settings,
                    candidate,
                )
            elif isinstance(application.state.assistant, CodexProvider):
                application.state.assistant = application.state.assistant.for_workspace(candidate)
        return project_info()

    @application.websocket("/api/file-events/{path:path}")
    async def watch_file(socket: WebSocket, path: str):
        fs = workspace()
        try:
            fs.resolve(path, must_exist=True)
        except Exception as exc:
            await socket.close(code=1008, reason=str(translate_error(exc).detail))
            return

        await socket.accept()
        last_version: str | None = None
        try:
            while True:
                try:
                    content, version = fs.read_versioned(path)
                    if version != last_version:
                        payload = FileContent(path=path, content=content, version=version)
                        await socket.send_json(payload.model_dump())
                        last_version = version
                except FileNotFoundError:
                    await socket.send_json({"path": path, "deleted": True})
                    await socket.close(code=1000)
                    return
                except FileChangedError:
                    pass
                try:
                    await asyncio.wait_for(socket.receive_text(), timeout=1)
                except TimeoutError:
                    pass
        except WebSocketDisconnect:
            return

    @application.get("/api/files/{path:path}", response_model=FileContent)
    async def get_file(path: str, fs: Workspace = Depends(workspace)):
        try:
            content, version = fs.read_versioned(path)
            return FileContent(path=path, content=content, version=version)
        except Exception as exc:
            raise translate_error(exc) from exc

    @application.put("/api/files/{path:path}", response_model=FileContent)
    async def put_file(path: str, body: FileContent, fs: Workspace = Depends(workspace)):
        if body.path != path:
            raise HTTPException(status_code=400, detail="Body path does not match URL")
        try:
            version = fs.write(path, body.content, body.version)
            return FileContent(path=path, content=body.content, version=version)
        except Exception as exc:
            raise translate_error(exc) from exc

    @application.post("/api/files", status_code=201)
    async def create_entry(body: CreateEntry, fs: Workspace = Depends(workspace)):
        try:
            fs.create(body.path, body.type, body.content)
            return {"ok": True}
        except Exception as exc:
            raise translate_error(exc) from exc

    @application.post("/api/files/apply", response_model=ApplyEditsResponse)
    async def apply_reviewed_edits(
        body: ApplyEditsRequest,
        fs: Workspace = Depends(workspace),
    ):
        try:
            async with application.state.project_lock:
                results = fs.apply_edits(
                    [
                        (edit.path, edit.content, edit.version, edit.is_new)
                        for edit in body.edits
                    ]
                )
            return ApplyEditsResponse(
                files=[
                    FileContent(path=path, content=content, version=version)
                    for path, content, version in results
                ]
            )
        except Exception as exc:
            raise translate_error(exc) from exc

    @application.patch("/api/files")
    async def rename_entry(body: RenameEntry, fs: Workspace = Depends(workspace)):
        try:
            fs.rename(body.path, body.new_path)
            return {"ok": True}
        except Exception as exc:
            raise translate_error(exc) from exc

    @application.delete("/api/files/{path:path}", status_code=204)
    async def delete_entry(path: str, fs: Workspace = Depends(workspace)):
        try:
            fs.delete(path)
            return Response(status_code=204)
        except Exception as exc:
            raise translate_error(exc) from exc

    @application.post("/api/compile", status_code=202)
    async def compile_project():
        return application.state.compiler.request()

    @application.get("/api/compile/status")
    async def compile_status():
        return application.state.compiler.status

    @application.get("/api/pdf")
    async def pdf():
        path = application.state.compiler.pdf_path
        if not path.exists():
            raise HTTPException(
                status_code=404,
                detail="No successful PDF build is available yet",
            )
        return FileResponse(
            path,
            media_type="application/pdf",
            headers={"Cache-Control": "no-store"},
        )

    @application.post("/api/assistant/chat", response_model=AssistantResponse)
    async def assistant_chat(body: AssistantRequest):
        queue: asyncio.Queue[AssistantProgress | None] | None = None
        if body.request_id is not None:
            queue = application.state.assistant_progress.get(body.request_id)
        activity_count = 0

        def report(phase: str, message: str) -> None:
            nonlocal activity_count
            if queue is None:
                return
            activity_count += 1
            queue.put_nowait(
                AssistantProgress(
                    phase=phase,
                    message=message,
                    activity_count=activity_count,
                )
            )

        try:
            return await run_assistant(body, report if queue is not None else None)
        finally:
            if queue is not None:
                queue.put_nowait(None)

    @application.websocket("/api/assistant/progress/{request_id}")
    async def assistant_progress(socket: WebSocket, request_id: UUID):
        request_key = str(request_id)
        queue: asyncio.Queue[AssistantProgress | None] = asyncio.Queue()
        application.state.assistant_progress[request_key] = queue
        current = AssistantProgress(
            phase="preparing",
            message="Preparing the open-file context…",
        )
        await socket.accept()
        await socket.send_json(current.model_dump())
        try:
            while True:
                try:
                    update = await asyncio.wait_for(
                        queue.get(), timeout=ASSISTANT_HEARTBEAT_SECONDS
                    )
                    if update is None:
                        await socket.close(code=1000)
                        return
                    current = update
                except TimeoutError:
                    current = current.model_copy(update={"heartbeat": True})
                await socket.send_json(current.model_dump())
        except (WebSocketDisconnect, RuntimeError):
            return
        finally:
            if application.state.assistant_progress.get(request_key) is queue:
                application.state.assistant_progress.pop(request_key, None)

    async def run_assistant(
        body: AssistantRequest,
        progress=None,
    ) -> AssistantResponse:
        try:
            async with application.state.project_lock:
                assistant = application.state.assistant
                if isinstance(assistant, CodexProvider):
                    active_workspace = application.state.workspace.root
                    if Path(assistant.workspace).resolve() != active_workspace:
                        assistant = assistant.for_workspace(active_workspace)
                        application.state.assistant = assistant
                compile_status = application.state.compiler.status.model_copy(deep=True)
                context = body.context.model_copy(
                    update={
                        "main_file": application.state.main_file,
                        "compile_state": compile_status.state,
                        "diagnostics": compile_status.diagnostics,
                        "compile_log": compile_status.log_tail,
                    }
                )
                if progress is not None:
                    response = await assistant.chat_with_progress(
                        body.messages, context, progress
                    )
                else:
                    response = await assistant.chat(body.messages, context)
                return prepare_assistant_edits(response, application.state.workspace)
        except AssistantUnavailable as exc:
            logger.warning("Assistant unavailable: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            logger.warning("Assistant provider HTTP failure: %s", exc)
            raise HTTPException(
                status_code=502,
                detail="The assistant provider could not be reached. Try again shortly.",
            ) from exc
        except Exception as exc:
            logger.exception("Unexpected assistant failure")
            raise HTTPException(
                status_code=503,
                detail="The assistant failed without changing any files.",
            ) from exc

    return application


def prepare_assistant_edits(
    response: AssistantResponse,
    workspace: Workspace,
) -> AssistantResponse:
    if len(response.proposed_edits) > 20:
        raise AssistantUnavailable("Codex returned too many file edits for one review.")

    prepared = []
    seen: set[str] = set()
    for edit in response.proposed_edits:
        try:
            target = workspace.resolve(edit.path)
            canonical_path = target.relative_to(workspace.root).as_posix()
            if canonical_path != edit.path or edit.path in seen:
                raise WorkspaceError("Edit paths must be unique canonical project paths")
            seen.add(edit.path)
            if edit.content is not None and edit.replacements:
                raise AssistantUnavailable(
                    "Codex returned two conflicting representations of the same edit."
                )
            if target.exists():
                if not target.is_file():
                    raise WorkspaceError("An edit target is not a file")
                current_content, version = workspace.read_versioned(edit.path)
                replacement_content = materialize_assistant_edit(
                    edit,
                    current_content=current_content,
                    is_new=False,
                )
                prepared.append(
                    edit.model_copy(
                        update={
                            "content": replacement_content,
                            "version": version,
                            "is_new": False,
                        }
                    )
                )
            else:
                replacement_content = materialize_assistant_edit(
                    edit,
                    current_content="",
                    is_new=True,
                )
                prepared.append(
                    edit.model_copy(
                        update={
                            "content": replacement_content,
                            "version": None,
                            "is_new": True,
                        }
                    )
                )
        except (OSError, WorkspaceError, UnicodeError) as exc:
            logger.warning("Rejected unsafe Codex edit path %r: %s", edit.path, exc)
            raise AssistantUnavailable(
                "Codex returned an edit outside Cloverleaf's safe project file boundary."
            ) from exc
    return response.model_copy(update={"proposed_edits": prepared})


def materialize_assistant_edit(
    edit: ProposedEdit,
    *,
    current_content: str,
    is_new: bool,
) -> str:
    """Expand a compact model edit into the complete content shown for review."""

    if edit.content is not None:
        content = edit.content
    elif is_new:
        if len(edit.replacements) != 1 or edit.replacements[0].old_text:
            raise AssistantUnavailable(
                f"Codex returned an invalid new-file edit for {edit.path}."
            )
        content = edit.replacements[0].new_text
    else:
        if not edit.replacements:
            raise AssistantUnavailable(
                f"Codex did not return a usable edit for {edit.path}."
            )
        content = current_content
        for replacement in edit.replacements:
            if not replacement.old_text or content.count(replacement.old_text) != 1:
                raise AssistantUnavailable(
                    f"Codex returned an edit for {edit.path} that did not uniquely match "
                    "the current file. Ask Codex to inspect the file and try again."
                )
            content = content.replace(
                replacement.old_text,
                replacement.new_text,
                1,
            )

    try:
        content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AssistantUnavailable(
            f"Codex returned invalid text for {edit.path}."
        ) from exc
    return content


def translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FileChangedError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (WorkspaceError, IsADirectoryError, NotADirectoryError)):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail="Path not found")
    if isinstance(exc, FileExistsError):
        return HTTPException(status_code=409, detail="Path already exists")
    logger.exception("Unexpected workspace failure", exc_info=exc)
    return HTTPException(status_code=500, detail="Filesystem operation failed")


app = create_app()
