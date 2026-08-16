from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TreeNode(BaseModel):
    name: str
    path: str
    type: Literal["file", "directory"]
    children: list[TreeNode] = Field(default_factory=list)


class FileContent(BaseModel):
    path: str
    content: str


class CreateEntry(BaseModel):
    path: str
    type: Literal["file", "directory"]
    content: str = ""


class RenameEntry(BaseModel):
    path: str
    new_path: str


class LoadProject(BaseModel):
    workspace: str
    main_file: str = "main.tex"


class ProjectInfo(BaseModel):
    workspace: str
    name: str
    main_file: str


class DirectoryEntry(BaseModel):
    name: str
    path: str


class DirectoryListing(BaseModel):
    path: str
    parent: str | None
    home: str
    root: str
    directories: list[DirectoryEntry] = Field(default_factory=list)
    tex_files: list[str] = Field(default_factory=list)


class Diagnostic(BaseModel):
    severity: Literal["error", "warning"]
    message: str
    file: str | None = None
    line: int | None = None


class CompileStatus(BaseModel):
    state: Literal["idle", "compiling", "success", "error"] = "idle"
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    log_tail: str = ""
    revision: int = 0


class AssistantMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class AssistantContext(BaseModel):
    open_file: str | None = None
    open_file_content: str = ""
    selected_text: str | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class ProposedEdit(BaseModel):
    path: str
    content: str
    summary: str


class AssistantRequest(BaseModel):
    messages: list[AssistantMessage]
    context: AssistantContext


class AssistantResponse(BaseModel):
    message: str
    proposed_edits: list[ProposedEdit] = Field(default_factory=list)
