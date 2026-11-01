from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
from .compiler import Compiler, find_latexmk
from .config import Settings, get_settings
from .models import (
    ApplyEditsRequest,
    ApplyEditsResponse,
    AssistantRequest,
    AssistantProgress,
    AssistantResponse,
    CreateEntry,
    CreateProject,
    DirectoryEntry,
    DirectoryListing,
    FileContent,
    LoadProject,
    ProjectInfo,
    ProjectAssistantHistory,
    ProposedEdit,
    PdfSourceRequest,
    SourceLocation,
    RepositoryInfo,
    RenameEntry,
    SelectCompilationRoot,
)
from .version_control import LocalGitRepository
from .project_sync import ProjectSync
from .workspace import FileChangedError, Workspace, WorkspaceBoundaryError, WorkspaceError


logger = logging.getLogger(__name__)
ASSISTANT_HEARTBEAT_SECONDS = 3
ASSISTANT_ATTACHMENT_MAX_BYTES = 1024 * 1024

DEFAULT_MAIN_TEX = """\\documentclass{article}
\\title{Untitled manuscript}
\\author{}
\\date{}

\\begin{document}
\\maketitle

Start writing here.

\\end{document}
"""

ASSISTANT_HISTORY_DIRECTORY = ".cloverleaf"
ASSISTANT_HISTORY_FILE = "codex-history.json"
MAX_ASSISTANT_HISTORY_BYTES = 10_000_000


def assistant_history_path(workspace: Path) -> Path:
    relative_path = f"{ASSISTANT_HISTORY_DIRECTORY}/{ASSISTANT_HISTORY_FILE}"
    try:
        return Workspace(workspace).resolve(relative_path)
    except WorkspaceBoundaryError as exc:
        raise OSError("Codex history location escapes the project") from exc


def load_assistant_history(workspace: Path) -> ProjectAssistantHistory:
    try:
        path = assistant_history_path(workspace)
        if not path.is_file():
            return ProjectAssistantHistory(workspace=str(workspace))
        history = ProjectAssistantHistory.model_validate_json(path.read_text(encoding="utf-8"))
        return history.model_copy(update={"workspace": str(workspace)})
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Ignoring invalid Codex history in %s: %s", workspace, exc)
        return ProjectAssistantHistory(workspace=str(workspace))


def persist_assistant_history(history: ProjectAssistantHistory, workspace: Path) -> None:
    encoded = history.model_copy(update={"workspace": str(workspace)}).model_dump_json(indent=2)
    if len(encoded.encode("utf-8")) > MAX_ASSISTANT_HISTORY_BYTES:
        raise ValueError("Codex history exceeds the 10 MB project limit")
    path = assistant_history_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(f"{encoded}\n", encoding="utf-8")
    temporary.replace(path)


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
        app.state.repository = LocalGitRepository(initial_workspace)
        app.state.repository.initialize()
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
        app.state.project_sync = ProjectSync(
            settings.projects_remote, settings.projects_sync_path,
            settings.workspace.parent, settings.projects_sync_interval,
        )
        await asyncio.to_thread(app.state.project_sync.register, initial_workspace)
        sync_task = asyncio.create_task(app.state.project_sync.run()) if settings.projects_remote else None
        app.state.assistant_progress = {}
        app.state.assistant_tasks = {}
        app.state.assistant_cancel_requests = set()
        try:
            yield
        finally:
            app.state.project_sync.stop()
            if sync_task:
                await sync_task
            tasks = list(app.state.assistant_tasks.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

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

    async def register_project_sync(root: Path) -> None:
        try:
            await asyncio.to_thread(application.state.project_sync.register, root)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def checkpoint(fs: Workspace, paths: list[str], message: str) -> None:
        repository = application.state.repository
        if repository.root != fs.root:
            logger.warning("Skipped checkpoint for an inactive project root: %s", fs.root)
            return
        repository.checkpoint(paths, message)

    @application.get("/api/health")
    async def health():
        return {"ok": True}

    @application.get("/api/status")
    async def service_status():
        settings = application.state.settings
        fs = application.state.workspace
        repository_status = application.state.repository.status()
        compiler_status = application.state.compiler.status
        assistant = application.state.assistant

        project_main = fs.root / application.state.main_file
        if not fs.root.is_dir():
            project_condition = "unavailable"
            project_detail = f"Project root is unavailable: {fs.root}"
        elif not project_main.is_file():
            project_condition = "degraded"
            project_detail = f"Compilation root is missing: {application.state.main_file}"
        else:
            project_condition = "healthy"
            project_detail = f"{fs.root} · compiling {application.state.main_file}"

        if repository_status.initialized:
            git_condition = "healthy"
            git_detail = f"Local checkpoints active · HEAD {repository_status.head or 'unborn'}"
        else:
            git_condition = "unavailable"
            git_detail = repository_status.last_error or "Local Git checkpoints are unavailable"

        latexmk = find_latexmk()
        if latexmk is None:
            compiler_condition = "unavailable"
            compiler_detail = "latexmk is not installed or is not on PATH"
        elif compiler_status.state == "error":
            compiler_condition = "degraded"
            compiler_detail = "The latest compilation failed"
        else:
            compiler_condition = "healthy"
            compiler_detail = f"latexmk available · build state {compiler_status.state}"

        if isinstance(assistant, CodexProvider):
            codex_executable = assistant.codex_bin or shutil.which("codex")
            sdk_available = importlib.util.find_spec("openai_codex") is not None
            if codex_executable and sdk_available:
                assistant_condition = "healthy"
                assistant_detail = f"Codex SDK and CLI available · model {assistant.model}"
            else:
                assistant_condition = "unavailable"
                missing = "Codex CLI" if not codex_executable else "Codex SDK"
                assistant_detail = f"{missing} is unavailable"
        elif isinstance(assistant, OpenAICompatibleProvider):
            assistant_condition = "healthy"
            assistant_detail = f"OpenAI-compatible provider configured · model {assistant.model}"
        else:
            assistant_condition = "unavailable"
            assistant_detail = "No assistant provider is configured"

        restart_path = settings.runtime_restart_path
        shutdown_path = settings.runtime_shutdown_path
        launcher_managed = (
            settings.runtime_log_path is not None
            and shutdown_path is not None
            and restart_path is not None
        )
        launcher_condition = "healthy" if launcher_managed else "degraded"
        launcher_detail = (
            "Managed launcher active · restart and shutdown available"
            if launcher_managed
            else "Unmanaged development process · launcher controls unavailable"
        )

        services = [
            {"id": "backend", "name": "Backend API", "condition": "healthy", "detail": "FastAPI is responding"},
            {"id": "project", "name": "Project workspace", "condition": project_condition, "detail": project_detail},
            {"id": "git", "name": "Git checkpoints", "condition": git_condition, "detail": git_detail},
            {"id": "compiler", "name": "LaTeX compiler", "condition": compiler_condition, "detail": compiler_detail},
            {"id": "assistant", "name": "Codex provider", "condition": assistant_condition, "detail": assistant_detail},
            {"id": "launcher", "name": "Managed launcher", "condition": launcher_condition, "detail": launcher_detail},
        ]
        sync_status = application.state.project_sync.status()
        if sync_status["enabled"]:
            services.append({
                "id": "sync", "name": "GitHub project sync",
                "condition": "degraded" if sync_status["last_error"] or not sync_status["last_synced"] else "healthy",
                "detail": sync_status["last_error"] or (
                    f"{len(sync_status['projects'])} projects · last synced {sync_status['last_synced']}"
                    if sync_status["last_synced"] else "Initial project sync in progress"
                ),
            })
        overall = "healthy" if all(item["condition"] == "healthy" for item in services) else "degraded"
        return {
            "overall": overall,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "services": services,
        }

    @application.get("/api/runtime")
    async def runtime_info():
        log_path = application.state.settings.runtime_log_path
        shutdown_path = application.state.settings.runtime_shutdown_path
        restart_path = application.state.settings.runtime_restart_path
        return {
            "managed": log_path is not None and shutdown_path is not None and restart_path is not None,
            "log_available": log_path is not None and log_path.is_file(),
            "shutdown_available": shutdown_path is not None,
            "restart_available": restart_path is not None,
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
                except asyncio.TimeoutError:
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

    @application.post("/api/runtime/restart", status_code=202)
    async def runtime_restart():
        restart_path = application.state.settings.runtime_restart_path
        if restart_path is None:
            raise HTTPException(
                status_code=409,
                detail="Restart is available only when Cloverleaf is started by its launcher",
            )
        if not restart_path.parent.is_dir():
            raise HTTPException(status_code=503, detail="The Cloverleaf launcher is unavailable")
        try:
            restart_path.write_text("restart requested\n", encoding="utf-8")
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail="Cloverleaf could not contact its launcher",
            ) from exc
        return {"ok": True, "message": "Server restart requested"}

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

    @application.get("/api/project/codex-history", response_model=ProjectAssistantHistory)
    async def get_project_assistant_history():
        return load_assistant_history(application.state.workspace.root)

    @application.put("/api/project/codex-history", response_model=ProjectAssistantHistory)
    async def put_project_assistant_history(body: ProjectAssistantHistory):
        active_workspace = application.state.workspace.root
        try:
            requested_workspace = Path(body.workspace).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail="Codex history workspace is invalid") from exc
        if requested_workspace != active_workspace:
            raise HTTPException(
                status_code=409,
                detail="Codex history belongs to a different active project",
            )
        try:
            persist_assistant_history(body, active_workspace)
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=503, detail="Codex history could not be saved") from exc
        return body.model_copy(update={"workspace": str(active_workspace)})

    @application.get("/api/project/git", response_model=RepositoryInfo)
    async def get_project_repository():
        return RepositoryInfo.model_validate(application.state.repository.status(), from_attributes=True)

    @application.get("/api/projects/sync")
    async def get_projects_sync():
        return application.state.project_sync.status()

    @application.post("/api/project/compilation-root", response_model=ProjectInfo)
    async def select_compilation_root(body: SelectCompilationRoot):
        async with application.state.project_lock:
            fs = application.state.workspace
            try:
                main_path = fs.resolve(body.main_file, must_exist=True)
                if not main_path.is_file():
                    raise IsADirectoryError(body.main_file)
                if main_path.suffix.lower() != ".tex":
                    raise WorkspaceError("Compilation root must be a relative .tex path")
            except Exception as exc:
                raise translate_error(exc) from exc

            main_file = main_path.relative_to(fs.root).as_posix()
            if main_file == application.state.main_file:
                return project_info()

            await application.state.compiler.finish()
            try:
                persist_project(application.state.project_state_path, fs.root, main_file)
            except OSError as exc:
                raise HTTPException(
                    status_code=500,
                    detail="The compilation root is valid, but Cloverleaf could not save it",
                ) from exc
            application.state.compiler = Compiler(fs.root, main_file)
            application.state.main_file = main_file
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
            await register_project_sync(candidate)
            try:
                persist_project(application.state.project_state_path, candidate, main_file)
            except OSError as exc:
                raise HTTPException(
                    status_code=500,
                    detail="The project is valid, but Cloverleaf could not save the selection",
                ) from exc
            next_repository = LocalGitRepository(candidate)
            next_repository.initialize()
            if create_main:
                next_repository.checkpoint([main_file], f"Create {main_file}")
            application.state.workspace = next_workspace
            application.state.repository = next_repository
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

    @application.post("/api/project/new", response_model=ProjectInfo, status_code=201)
    async def create_project(body: CreateProject):
        raw_parent = body.parent.strip()
        parent = Path(raw_parent).expanduser()
        if not raw_parent or not parent.is_absolute():
            raise HTTPException(
                status_code=400,
                detail="Project parent directory must be an absolute path",
            )
        parent = parent.resolve()
        if not parent.exists():
            raise HTTPException(status_code=404, detail="Project parent directory does not exist")
        if not parent.is_dir():
            raise HTTPException(status_code=400, detail="Project parent path must be a directory")

        name: str | None = None
        if body.name is not None:
            name = body.name.strip()
            if (
                not name
                or name in {".", ".."}
                or name.startswith(".")
                or "/" in name
                or "\\" in name
                or "\x00" in name
            ):
                raise HTTPException(
                    status_code=400,
                    detail="New folder name must be one visible directory name",
                )

        candidate = parent / name if name else parent
        created_directory = False
        created_main = False
        main_path = candidate / "main.tex"

        async with application.state.project_lock:
            await application.state.compiler.finish()
            try:
                if name:
                    candidate.mkdir()
                    created_directory = True
                if main_path.exists():
                    raise HTTPException(
                        status_code=409,
                        detail="main.tex already exists; use Load project instead",
                    )
                next_workspace = Workspace(candidate)
                next_workspace.create("main.tex", "file", DEFAULT_MAIN_TEX)
                created_main = True
                await register_project_sync(candidate)
                persist_project(application.state.project_state_path, candidate, "main.tex")
            except HTTPException:
                if created_main:
                    main_path.unlink(missing_ok=True)
                if created_directory:
                    candidate.rmdir()
                raise
            except FileExistsError as exc:
                if created_main:
                    main_path.unlink(missing_ok=True)
                if created_directory:
                    candidate.rmdir()
                raise HTTPException(
                    status_code=409,
                    detail="A folder with that name already exists",
                ) from exc
            except PermissionError as exc:
                if created_main:
                    main_path.unlink(missing_ok=True)
                if created_directory:
                    candidate.rmdir()
                raise HTTPException(
                    status_code=403,
                    detail="Project directory is not writable",
                ) from exc
            except OSError as exc:
                if created_main:
                    main_path.unlink(missing_ok=True)
                if created_directory:
                    candidate.rmdir()
                raise HTTPException(
                    status_code=500,
                    detail="Cloverleaf could not create the project",
                ) from exc

            next_repository = LocalGitRepository(candidate)
            next_repository.initialize()
            next_repository.checkpoint(["main.tex"], "Create main.tex")
            application.state.workspace = next_workspace
            application.state.repository = next_repository
            application.state.compiler = Compiler(candidate, "main.tex")
            application.state.main_file = "main.tex"
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
                except asyncio.TimeoutError:
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
            checkpoint(fs, [path], f"Update {path}")
            return FileContent(path=path, content=body.content, version=version)
        except Exception as exc:
            raise translate_error(exc) from exc

    @application.post("/api/files", status_code=201)
    async def create_entry(body: CreateEntry, fs: Workspace = Depends(workspace)):
        try:
            fs.create(body.path, body.type, body.content)
            checkpoint(fs, [body.path], f"Create {body.path}")
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
                checkpoint(
                    fs,
                    [path for path, _content, _version in results],
                    f"Apply {len(results)} reviewed Codex edit{'s' if len(results) != 1 else ''}",
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
            checkpoint(fs, [body.path, body.new_path], f"Rename {body.path} to {body.new_path}")
            return {"ok": True}
        except Exception as exc:
            raise translate_error(exc) from exc

    @application.delete("/api/files/{path:path}", status_code=204)
    async def delete_entry(path: str, fs: Workspace = Depends(workspace)):
        try:
            fs.delete(path)
            checkpoint(fs, [path], f"Delete {path}")
            return Response(status_code=204)
        except Exception as exc:
            raise translate_error(exc) from exc

    @application.post("/api/compile", status_code=202)
    async def compile_project():
        return application.state.compiler.request()

    @application.get("/api/compile/status")
    async def compile_status():
        return application.state.compiler.status

    @application.post("/api/pdf/source", response_model=SourceLocation)
    async def pdf_source(body: PdfSourceRequest):
        async with application.state.project_lock:
            fs = application.state.workspace
            compiler = application.state.compiler
            if (
                body.workspace != str(fs.root) or body.main_file != compiler.main_file
                or body.revision != compiler.status.revision or compiler.status.state != "success"
            ):
                raise HTTPException(status_code=409, detail="The preview changed; wait for a successful compile and try again")
            try:
                source, line, column = await compiler.source_location(body.page, body.x, body.y)
                if compiler.status.state != "success" or compiler.status.revision != body.revision:
                    raise HTTPException(status_code=409, detail="The preview changed during source lookup; try again after compilation")
                target, relative = fs.resolve_assistant_path(source)
                fs.resolve(relative, must_exist=True)
                if not target.is_file():
                    raise ValueError("The mapped source is not a file")
                return SourceLocation(path=relative, line=line, column=column)
            except WorkspaceBoundaryError as exc:
                raise HTTPException(status_code=400, detail="The mapped source is outside the active project") from exc
            except (FileNotFoundError, OSError, TimeoutError) as exc:
                raise HTTPException(status_code=503, detail=str(exc) or "Source lookup timed out") from exc
            except (ValueError, WorkspaceError) as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

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
            request_key = body.request_id
            if request_key is None:
                return await run_assistant(body, report if queue is not None else None)
            if request_key in application.state.assistant_tasks:
                raise HTTPException(status_code=409, detail="This Codex turn is already running")
            if request_key in application.state.assistant_cancel_requests:
                application.state.assistant_cancel_requests.discard(request_key)
                raise HTTPException(status_code=409, detail="Codex turn was cancelled")

            turn_task = asyncio.create_task(
                run_assistant(body, report if queue is not None else None)
            )
            application.state.assistant_tasks[request_key] = turn_task
            try:
                return await turn_task
            except asyncio.CancelledError:
                if request_key in application.state.assistant_cancel_requests:
                    raise HTTPException(status_code=409, detail="Codex turn was cancelled")
                raise
            finally:
                application.state.assistant_tasks.pop(request_key, None)
                application.state.assistant_cancel_requests.discard(request_key)
        finally:
            if queue is not None:
                queue.put_nowait(None)

    @application.post("/api/assistant/cancel/{request_id}", status_code=202)
    async def assistant_cancel(request_id: UUID):
        request_key = str(request_id)
        task = application.state.assistant_tasks.get(request_key)
        if task is None and request_key not in application.state.assistant_progress:
            raise HTTPException(status_code=404, detail="Codex turn is not running")
        application.state.assistant_cancel_requests.add(request_key)
        if task is not None and not task.done():
            task.cancel()
        return {"ok": True, "message": "Codex turn cancellation requested"}

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
                attached_files = []
                seen_attachments: set[str] = set()
                for attachment_path in body.attached_paths:
                    if attachment_path in seen_attachments:
                        continue
                    seen_attachments.add(attachment_path)
                    try:
                        content, version = application.state.workspace.read_versioned(
                            attachment_path, max_bytes=ASSISTANT_ATTACHMENT_MAX_BYTES
                        )
                    except Exception as exc:
                        raise translate_error(exc) from exc
                    attached_files.append(
                        FileContent(path=attachment_path, content=content, version=version)
                    )
                context = body.context.model_copy(
                    update={
                        "main_file": application.state.main_file,
                        "compile_state": compile_status.state,
                        "diagnostics": compile_status.diagnostics,
                        "compile_log": compile_status.log_tail,
                        "attached_files": attached_files,
                    }
                )
                if progress is not None:
                    response = await assistant.chat_with_progress(
                        body.messages, context, progress
                    )
                else:
                    response = await assistant.chat(body.messages, context)
                return prepare_assistant_edits(response, application.state.workspace)
        except HTTPException:
            raise
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
    warnings = list(response.warnings)
    for edit in response.proposed_edits:
        try:
            target, canonical_path = workspace.resolve_assistant_path(edit.path)
        except (OSError, WorkspaceBoundaryError) as exc:
            logger.warning("Rejected out-of-project Codex edit path %r: %s", edit.path, exc)
            display_path = edit.path if len(edit.path) <= 240 else f"{edit.path[:237]}…"
            warnings.append(
                "Cloverleaf rejected an edit outside the active project or inside its "
                f'protected .git directory: "{display_path}".'
            )
            continue

        if canonical_path in seen:
            warnings.append(
                f'Cloverleaf ignored a duplicate proposed edit for "{canonical_path}".'
            )
            continue
        seen.add(canonical_path)
        canonical_edit = edit.model_copy(update={"path": canonical_path})
        try:
            if edit.content is not None and edit.replacements:
                raise AssistantUnavailable(
                    "Codex returned two conflicting representations of the same edit."
                )
            if target.exists():
                if not target.is_file():
                    raise WorkspaceError("An edit target is not a file")
                current_content, version = workspace.read_versioned(canonical_path)
                replacement_content = materialize_assistant_edit(
                    canonical_edit,
                    current_content=current_content,
                    is_new=False,
                )
                prepared.append(
                    canonical_edit.model_copy(
                        update={
                            "content": replacement_content,
                            "version": version,
                            "is_new": False,
                        }
                    )
                )
            else:
                replacement_content = materialize_assistant_edit(
                    canonical_edit,
                    current_content="",
                    is_new=True,
                )
                prepared.append(
                    canonical_edit.model_copy(
                        update={
                            "content": replacement_content,
                            "version": None,
                            "is_new": True,
                        }
                    )
                )
        except (OSError, WorkspaceError, UnicodeError) as exc:
            logger.warning("Rejected unusable in-project Codex edit %r: %s", edit.path, exc)
            warnings.append(
                f'Cloverleaf could not prepare the proposed edit for "{canonical_path}": {exc}.'
            )
    return response.model_copy(update={"proposed_edits": prepared, "warnings": warnings})


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
