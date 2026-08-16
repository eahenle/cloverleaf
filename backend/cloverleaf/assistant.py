from __future__ import annotations

import json
import logging
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

import httpx

from .models import AssistantContext, AssistantMessage, AssistantResponse, ProposedEdit


logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, str], None]

CODEX_MAX_PROMPT_CHARS = 700_000
OPEN_FILE_MAX_CHARS = 300_000
SELECTED_TEXT_MAX_CHARS = 100_000
DIAGNOSTICS_MAX_CHARS = 40_000
CONVERSATION_MAX_CHARS = 120_000


class AssistantProvider(ABC):
    @abstractmethod
    async def chat(
        self, messages: list[AssistantMessage], context: AssistantContext
    ) -> AssistantResponse:
        raise NotImplementedError

    async def chat_with_progress(
        self,
        messages: list[AssistantMessage],
        context: AssistantContext,
        progress: ProgressCallback,
    ) -> AssistantResponse:
        progress("waiting", "Waiting for the assistant provider…")
        return await self.chat(messages, context)


class AssistantUnavailable(RuntimeError):
    pass


def _truncate_middle(value: str, limit: int, label: str) -> str:
    if len(value) <= limit:
        return value
    marker = f"\n… [{len(value) - limit:,} characters omitted from {label}] …\n"
    available = max(0, limit - len(marker))
    before = (available * 2) // 3
    return value[:before] + marker + value[-(available - before) :]


def _truncate_start(value: str, limit: int, label: str) -> str:
    if len(value) <= limit:
        return value
    marker = f"… [{len(value) - limit:,} earlier characters omitted from {label}] …\n"
    return marker + value[-max(0, limit - len(marker)) :]


def build_codex_prompt(
    messages: list[AssistantMessage],
    context: AssistantContext,
    *,
    can_inspect_workspace: bool = True,
    workspace_root: str | None = None,
) -> str:
    transcript = "\n\n".join(
        f"{message.role.upper()}: {message.content}" for message in messages
    )
    transcript = _truncate_start(transcript, CONVERSATION_MAX_CHARS, "conversation")
    open_file_content = _truncate_middle(
        context.open_file_content, OPEN_FILE_MAX_CHARS, "open file"
    )
    selected_text = _truncate_middle(
        context.selected_text or "(none)", SELECTED_TEXT_MAX_CHARS, "selection"
    )
    diagnostics = _truncate_middle(
        json.dumps(
            [diagnostic.model_dump() for diagnostic in context.diagnostics],
            indent=2,
        ),
        DIAGNOSTICS_MAX_CHARS,
        "compiler diagnostics",
    )
    runtime_guidance = (
        "Inspect files in the current workspace with read-only tools whenever more context is needed; "
        "Cloverleaf intentionally does not serialize the project tree into the request. "
        if can_inspect_workspace
        else "Use the supplied active-file context; no project tree is serialized into the request. "
    )
    prompt = (
        "You are the manuscript assistant embedded in Cloverleaf, a local LaTeX editor. "
        "Answer the latest user request using the project context. Be technically precise and concise. "
        "You are in a read-only sandbox: do not edit files or claim changes were applied. "
        f"{runtime_guidance}"
        "If a complete-file edit is useful, append a fenced JSON block tagged cloverleaf-edits "
        "containing an array of objects with path, content, and summary.\n\n"
        f"WORKSPACE ROOT: {workspace_root or '(not available)'}\n"
        f"OPEN FILE: {context.open_file or '(none)'}\n"
        f"OPEN FILE CONTENT:\n{open_file_content}\n\n"
        f"SELECTED TEXT:\n{selected_text}\n\n"
        f"COMPILER DIAGNOSTICS:\n{diagnostics}\n\n"
        f"CONVERSATION:\n{transcript}"
    )
    if len(prompt) > CODEX_MAX_PROMPT_CHARS:
        raise AssistantUnavailable(
            "The assistant request is too large even after compacting the project context. "
            "Open a smaller source file or start a new assistant conversation and try again."
        )
    return prompt


def _codex_failure_message(exc: Exception) -> str:
    detail = str(exc).lower()
    if "maximum length" in detail or "too large" in detail:
        return (
            "The assistant request is too large. Open a smaller source file or start a new "
            "assistant conversation and try again."
        )
    if "timed out" in detail or "timeout" in detail:
        return "Codex timed out while answering. Try again shortly."
    if any(term in detail for term in ("unauthorized", "authentication", "not logged in")):
        return "Codex authentication is unavailable. Run `codex login` and try again."
    if isinstance(exc, FileNotFoundError):
        return "The Codex executable could not be started. Run `make install` and try again."
    return "Codex could not answer. Check the backend log for details and try again."


class UnconfiguredProvider(AssistantProvider):
    async def chat(
        self, messages: list[AssistantMessage], context: AssistantContext
    ) -> AssistantResponse:
        raise AssistantUnavailable(
            "The assistant is not configured. Check AI_PROVIDER and its credentials, then restart Cloverleaf."
        )


class CodexProvider(AssistantProvider):
    """Read-only Codex adapter; Cloverleaf remains responsible for applying edits."""

    def __init__(self, model: str, workspace: str, codex_bin: str | None = None):
        self.model = model
        self.workspace = workspace
        self.codex_bin = codex_bin

    async def chat(
        self, messages: list[AssistantMessage], context: AssistantContext
    ) -> AssistantResponse:
        return await self.chat_with_progress(messages, context, lambda _phase, _message: None)

    async def chat_with_progress(
        self,
        messages: list[AssistantMessage],
        context: AssistantContext,
        progress: ProgressCallback,
    ) -> AssistantResponse:
        try:
            from openai_codex import AsyncCodex, CodexConfig, Sandbox
        except ImportError as exc:
            raise AssistantUnavailable(
                "The Codex SDK is not installed. Run `make install` and restart Cloverleaf."
            ) from exc

        prompt = build_codex_prompt(messages, context, workspace_root=self.workspace)
        try:
            progress("launching", "Launching the Codex runtime…")
            local_codex = self.codex_bin or shutil.which("codex")
            config = CodexConfig(codex_bin=local_codex) if local_codex else None
            async with AsyncCodex(config) as codex:
                progress("connecting", "Connecting to Codex…")
                thread = await codex.thread_start(
                    model=self.model,
                    sandbox=Sandbox.read_only,
                    cwd=self.workspace,
                )
                progress("submitting", "Sending the manuscript request…")
                turn = await thread.turn(prompt)
                progress("working", "Codex accepted the request and is working…")
                final_response = await _consume_codex_turn(turn, progress)
        except Exception as exc:
            logger.warning("Codex request failed (%s): %s", type(exc).__name__, exc)
            raise AssistantUnavailable(_codex_failure_message(exc)) from exc
        return parse_response(final_response)

    def for_workspace(self, workspace: Path) -> CodexProvider:
        return CodexProvider(self.model, str(workspace), self.codex_bin)


def _thread_item(item: object) -> object:
    return getattr(item, "root", item)


def _item_type(item: object) -> str | None:
    value = getattr(_thread_item(item), "type", None)
    return value if isinstance(value, str) else None


def codex_event_progress(event: object) -> tuple[str, str] | None:
    """Map detailed SDK notifications to stable, non-sensitive UI phases."""

    method = getattr(event, "method", "")
    payload = getattr(event, "payload", None)
    if method == "turn/started":
        return "working", "Codex started analyzing the request…"
    if method == "error" and getattr(payload, "will_retry", False):
        return "retrying", "Codex hit a transient problem and is retrying…"
    if method != "item/started":
        return None

    kind = _item_type(getattr(payload, "item", None))
    if kind == "reasoning":
        return "analyzing", "Analyzing the manuscript and project context…"
    if kind == "commandExecution":
        return "inspecting", "Inspecting project files in the read-only workspace…"
    if kind in {"mcpToolCall", "dynamicToolCall", "webSearch"}:
        return "tools", "Consulting a project tool…"
    if kind == "plan":
        return "planning", "Planning the response…"
    if kind == "agentMessage":
        return "responding", "Drafting the response…"
    return None


def _final_codex_response(items: list[object]) -> str | None:
    fallback: str | None = None
    for wrapped in reversed(items):
        item = _thread_item(wrapped)
        if _item_type(item) != "agentMessage":
            continue
        text = getattr(item, "text", None)
        if not isinstance(text, str):
            continue
        phase = getattr(getattr(item, "phase", None), "value", None)
        if phase == "final_answer":
            return text
        if phase is None and fallback is None:
            fallback = text
    return fallback


async def _consume_codex_turn(turn: object, progress: ProgressCallback) -> str:
    items: list[object] = []
    completed_turn: object | None = None
    async for event in turn.stream():
        next_progress = codex_event_progress(event)
        if next_progress is not None:
            progress(*next_progress)
        if getattr(event, "method", "") == "item/completed":
            item = getattr(getattr(event, "payload", None), "item", None)
            if item is not None:
                items.append(item)
        elif getattr(event, "method", "") == "turn/completed":
            completed_turn = getattr(getattr(event, "payload", None), "turn", None)

    if completed_turn is None:
        raise RuntimeError("turn completed event not received")
    status = getattr(getattr(completed_turn, "status", None), "value", None)
    if status != "completed":
        error = getattr(completed_turn, "error", None)
        message = getattr(error, "message", None)
        raise RuntimeError(message or f"turn ended with status {status or 'unknown'}")
    final_response = _final_codex_response(items)
    if final_response is None:
        raise RuntimeError("turn completed without a final response")
    progress("complete", "Codex completed the response.")
    return final_response


class OpenAICompatibleProvider(AssistantProvider):
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def chat(
        self, messages: list[AssistantMessage], context: AssistantContext
    ) -> AssistantResponse:
        prompt = build_codex_prompt(messages, context, can_inspect_workspace=False)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are Cloverleaf's manuscript assistant. Use the supplied project context. "
                        "Never claim to apply edits. Proposed complete-file edits must be a fenced "
                        "cloverleaf-edits JSON array with path, content, and summary."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            response.raise_for_status()
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AssistantUnavailable("The assistant provider returned an invalid response.") from exc
        return parse_response(content)


def parse_response(content: str) -> AssistantResponse:
    marker = "```cloverleaf-edits"
    if marker not in content:
        return AssistantResponse(message=content.strip())

    visible, encoded = content.split(marker, 1)
    if "```" not in encoded:
        return AssistantResponse(message=content.strip())
    raw, remainder = encoded.split("```", 1)
    try:
        decoded = json.loads(raw.strip())
        if not isinstance(decoded, list):
            raise ValueError("Proposed edits must be a list")
        edits = [ProposedEdit.model_validate(item) for item in decoded]
    except (ValueError, TypeError):
        return AssistantResponse(message=content.strip())
    message = "\n".join(part for part in (visible.strip(), remainder.strip()) if part)
    return AssistantResponse(message=message, proposed_edits=edits)
