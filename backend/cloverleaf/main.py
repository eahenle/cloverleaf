from __future__ import annotations

import logging
from contextlib import asynccontextmanager

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
    FileContent,
    RenameEntry,
)
from .workspace import Workspace, WorkspaceError


logger = logging.getLogger(__name__)


def build_assistant(settings: Settings) -> AssistantProvider:
    if settings.ai_provider == "codex":
        return CodexProvider(
            settings.ai_model,
            str(settings.workspace),
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
