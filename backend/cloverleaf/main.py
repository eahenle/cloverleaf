from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Response
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
    AssistantRequest,
    AssistantResponse,
    CreateEntry,
    DirectoryEntry,
    DirectoryListing,
    FileContent,
    LoadProject,
    ProjectInfo,
    RenameEntry,
)
from .workspace import Workspace, WorkspaceError


logger = logging.getLogger(__name__)

DEFAULT_MAIN_TEX = """\\documentclass{article}
\\title{Untitled manuscript}
\\author{}
\\date{}

\\begin{document}
\\maketitle

Start writing here.

\\end{document}
"""


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
        app.state.workspace = Workspace(settings.workspace)
        app.state.compiler = Compiler(settings.workspace, settings.main_file)
        app.state.assistant = configured_assistant or build_assistant(settings)
        app.state.settings = settings
        app.state.main_file = settings.main_file
        app.state.project_lock = asyncio.Lock()
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

    @application.get("/api/files/{path:path}", response_model=FileContent)
    async def get_file(path: str, fs: Workspace = Depends(workspace)):
        try:
            return FileContent(path=path, content=fs.read(path))
        except Exception as exc:
            raise translate_error(exc) from exc

    @application.put("/api/files/{path:path}", response_model=FileContent)
    async def put_file(path: str, body: FileContent, fs: Workspace = Depends(workspace)):
        if body.path != path:
            raise HTTPException(status_code=400, detail="Body path does not match URL")
        try:
            fs.write(path, body.content)
            return body
        except Exception as exc:
            raise translate_error(exc) from exc

    @application.post("/api/files", status_code=201)
    async def create_entry(body: CreateEntry, fs: Workspace = Depends(workspace)):
        try:
            fs.create(body.path, body.type, body.content)
            return {"ok": True}
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
        try:
            return await application.state.assistant.chat(body.messages, body.context)
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


def translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (WorkspaceError, IsADirectoryError, NotADirectoryError)):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail="Path not found")
    if isinstance(exc, FileExistsError):
        return HTTPException(status_code=409, detail="Path already exists")
    logger.exception("Unexpected workspace failure", exc_info=exc)
    return HTTPException(status_code=500, detail="Filesystem operation failed")


app = create_app()
