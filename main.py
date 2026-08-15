from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .assistant import CodexProvider, OpenAICompatibleProvider, UnconfiguredProvider
from .compiler import Compiler
from .config import get_settings
from .models import AssistantRequest, AssistantResponse, CreateEntry, FileContent, RenameEntry
from .workspace import Workspace, WorkspaceError


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.workspace = Workspace(settings.workspace)
    app.state.compiler = Compiler(settings.workspace, settings.main_file)
    if settings.ai_provider == "codex":
        app.state.assistant = CodexProvider(settings.ai_model, str(settings.workspace))
    elif settings.ai_provider == "openai-compatible" and settings.ai_api_key:
        app.state.assistant = OpenAICompatibleProvider(settings.ai_base_url, settings.ai_api_key, settings.ai_model)
    else:
        app.state.assistant = UnconfiguredProvider()
    yield


app = FastAPI(title="Cloverleaf", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)


def workspace() -> Workspace:
    return app.state.workspace


def translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (WorkspaceError, IsADirectoryError, NotADirectoryError)):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail="Path not found")
    if isinstance(exc, FileExistsError):
        return HTTPException(status_code=409, detail="Path already exists")
    return HTTPException(status_code=500, detail="Filesystem operation failed")


@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/api/project/tree")
async def get_tree(fs: Workspace = Depends(workspace)):
    return fs.tree()


@app.get("/api/files/{path:path}", response_model=FileContent)
async def get_file(path: str, fs: Workspace = Depends(workspace)):
    try:
        return FileContent(path=path, content=fs.read(path))
    except Exception as exc:
        raise translate_error(exc) from exc


@app.put("/api/files/{path:path}", response_model=FileContent)
async def put_file(path: str, body: FileContent, fs: Workspace = Depends(workspace)):
    if body.path != path:
        raise HTTPException(status_code=400, detail="Body path does not match URL")
    try:
        fs.write(path, body.content)
        return body
    except Exception as exc:
        raise translate_error(exc) from exc


@app.post("/api/files", status_code=201)
async def create_entry(body: CreateEntry, fs: Workspace = Depends(workspace)):
    try:
        fs.create(body.path, body.type, body.content)
        return {"ok": True}
    except Exception as exc:
        raise translate_error(exc) from exc


@app.patch("/api/files")
async def rename_entry(body: RenameEntry, fs: Workspace = Depends(workspace)):
    try:
        fs.rename(body.path, body.new_path)
        return {"ok": True}
    except Exception as exc:
        raise translate_error(exc) from exc


@app.delete("/api/files/{path:path}", status_code=204)
async def delete_entry(path: str, fs: Workspace = Depends(workspace)):
    try:
        fs.delete(path)
        return Response(status_code=204)
    except Exception as exc:
        raise translate_error(exc) from exc


@app.post("/api/compile", status_code=202)
async def compile_project():
    return app.state.compiler.request()


@app.get("/api/compile/status")
async def compile_status():
    return app.state.compiler.status


@app.get("/api/pdf")
async def pdf():
    path = app.state.compiler.pdf_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="No compiled PDF is available")
    return FileResponse(path, media_type="application/pdf", headers={"Cache-Control": "no-store"})


@app.post("/api/assistant/chat", response_model=AssistantResponse)
async def assistant_chat(body: AssistantRequest):
    try:
        return await app.state.assistant.chat(body.messages, body.context)
    except (httpx.HTTPError, KeyError, IndexError) as exc:
        raise HTTPException(status_code=502, detail=f"Assistant provider error: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
