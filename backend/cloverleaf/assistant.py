from __future__ import annotations

import json
import shutil
from abc import ABC, abstractmethod

import httpx

from .models import AssistantContext, AssistantMessage, AssistantResponse, ProposedEdit


class AssistantProvider(ABC):
    @abstractmethod
    async def chat(
        self, messages: list[AssistantMessage], context: AssistantContext
    ) -> AssistantResponse:
        raise NotImplementedError


class AssistantUnavailable(RuntimeError):
    pass


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
        try:
            from openai_codex import AsyncCodex, CodexConfig, Sandbox
        except ImportError as exc:
            raise AssistantUnavailable(
                "The Codex SDK is not installed. Run `make install` and restart Cloverleaf."
            ) from exc

        transcript = "\n\n".join(
            f"{message.role.upper()}: {message.content}" for message in messages
        )
        prompt = (
            "You are the manuscript assistant embedded in Cloverleaf, a local LaTeX editor. "
            "Answer the latest user request using the project context. Be technically precise and concise. "
            "You are in a read-only sandbox: do not edit files or claim changes were applied. "
            "If a complete-file edit is useful, append a fenced JSON block tagged cloverleaf-edits "
            "containing an array of objects with path, content, and summary.\n\n"
            f"PROJECT CONTEXT:\n{context.model_dump_json(indent=2)}\n\n"
            f"CONVERSATION:\n{transcript}"
        )
        try:
            local_codex = self.codex_bin or shutil.which("codex")
            config = CodexConfig(codex_bin=local_codex) if local_codex else None
            async with AsyncCodex(config) as codex:
                thread = await codex.thread_start(
                    model=self.model,
                    sandbox=Sandbox.read_only,
                    cwd=self.workspace,
                )
                result = await thread.run(prompt)
        except Exception as exc:
            raise AssistantUnavailable(
                "Codex could not answer. Confirm `codex login` is complete and try again."
            ) from exc
        return parse_response(result.final_response)


class OpenAICompatibleProvider(AssistantProvider):
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def chat(
        self, messages: list[AssistantMessage], context: AssistantContext
    ) -> AssistantResponse:
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
                {"role": "system", "content": context.model_dump_json(indent=2)},
                *[message.model_dump() for message in messages],
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
