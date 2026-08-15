import json
from abc import ABC, abstractmethod

import httpx

from .models import AssistantContext, AssistantMessage, AssistantResponse, ProposedEdit


class AssistantProvider(ABC):
    @abstractmethod
    async def chat(self, messages: list[AssistantMessage], context: AssistantContext) -> AssistantResponse:
        raise NotImplementedError


class UnconfiguredProvider(AssistantProvider):
    async def chat(self, messages: list[AssistantMessage], context: AssistantContext) -> AssistantResponse:
        return AssistantResponse(message="The assistant is not configured. Set AI_API_KEY, AI_BASE_URL, and AI_MODEL in .env, then restart the backend.")


class CodexProvider(AssistantProvider):
    """Read-only Codex adapter; Cloverleaf remains responsible for applying edits."""

    def __init__(self, model: str, workspace: str):
        self.model = model
        self.workspace = workspace

    async def chat(self, messages: list[AssistantMessage], context: AssistantContext) -> AssistantResponse:
        try:
            from openai_codex import AsyncCodex, Sandbox
        except ImportError as exc:
            raise RuntimeError("Codex SDK is not installed; run `make install`") from exc

        transcript = "\n\n".join(f"{message.role.upper()}: {message.content}" for message in messages)
        prompt = (
            "You are the manuscript assistant embedded in Cloverleaf, a local LaTeX editor. "
            "Answer the latest user request using the project context. Be technically precise and concise. "
            "Do not edit files or claim changes were applied. If a complete-file edit is useful, append a fenced "
            "JSON block tagged cloverleaf-edits containing an array of objects with path, content, and summary.\n\n"
            f"PROJECT CONTEXT:\n{context.model_dump_json(indent=2)}\n\nCONVERSATION:\n{transcript}"
        )
        async with AsyncCodex() as codex:
            thread = await codex.thread_start(
                model=self.model,
                sandbox=Sandbox.read_only,
                cwd=self.workspace,
            )
            result = await thread.run(prompt)
        return parse_response(result.final_response)


class OpenAICompatibleProvider(AssistantProvider):
    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def chat(self, messages: list[AssistantMessage], context: AssistantContext) -> AssistantResponse:
        system = (
            "You are Cloverleaf's manuscript assistant. Be precise and concise. Use the supplied project context. "
            "You may propose complete-file edits, but never claim to have applied them. To propose edits, append a fenced "
            "JSON block tagged cloverleaf-edits with an array of objects containing path, content, and summary."
        )
        context_message = "Project context:\n" + context.model_dump_json(indent=2)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "system", "content": context_message},
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
            content = response.json()["choices"][0]["message"]["content"]
        return parse_response(content)


def parse_response(content: str) -> AssistantResponse:
    edits: list[ProposedEdit] = []
    marker = "```cloverleaf-edits"
    if marker in content:
        visible, encoded = content.split(marker, 1)
        try:
            raw = encoded.split("```", 1)[0].strip()
            edits = [ProposedEdit.model_validate(item) for item in json.loads(raw)]
            content = visible.strip()
        except (ValueError, KeyError):
            pass
    return AssistantResponse(message=content, proposed_edits=edits)
