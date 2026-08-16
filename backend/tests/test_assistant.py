import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from cloverleaf.assistant import (
    CODEX_MAX_PROMPT_CHARS,
    AssistantProvider,
    OpenAICompatibleProvider,
    build_codex_prompt,
    parse_response,
)
from cloverleaf.config import Settings
from cloverleaf.main import create_app
from cloverleaf.models import AssistantContext, AssistantMessage, AssistantResponse


def test_parse_proposed_edits() -> None:
    payload = [
        {
            "path": "sections/introduction.tex",
            "content": "Revised text",
            "summary": "Tighten the opening",
        }
    ]
    response = parse_response(
        "Here is a revision.\n```cloverleaf-edits\n"
        + json.dumps(payload)
        + "\n```\nReview it before applying."
    )

    assert response.message == "Here is a revision.\nReview it before applying."
    assert response.proposed_edits[0].path == "sections/introduction.tex"


def test_malformed_edit_block_remains_visible() -> None:
    content = "Explanation\n```cloverleaf-edits\nnot json\n```"
    response = parse_response(content)
    assert response.message == content
    assert response.proposed_edits == []


def test_codex_prompt_sends_open_file_without_project_tree() -> None:
    context = AssistantContext(
        open_file="main.tex",
        open_file_content="A" * 400_000 + "END OF MANUSCRIPT",
        selected_text="important selection",
    )

    prompt = build_codex_prompt(
        [AssistantMessage(role="user", content="Explain the manuscript")], context
    )

    assert len(prompt) <= CODEX_MAX_PROMPT_CHARS
    assert "main.tex" in prompt
    assert "important selection" in prompt
    assert "END OF MANUSCRIPT" in prompt
    assert "Explain the manuscript" in prompt
    assert "PROJECT TREE" not in prompt
    assert "Inspect files in the current workspace" in prompt


def test_assistant_prompt_keeps_latest_message_with_large_history() -> None:
    messages = [
        AssistantMessage(role="user", content=f"old request {index} " + "x" * 20_000)
        for index in range(100)
    ]
    messages.append(AssistantMessage(role="user", content="LATEST REQUEST MUST SURVIVE"))
    context = AssistantContext(
        open_file="main.tex",
        open_file_content="manuscript",
    )

    prompt = build_codex_prompt(messages, context)

    assert len(prompt) <= CODEX_MAX_PROMPT_CHARS
    assert "LATEST REQUEST MUST SURVIVE" in prompt
    assert "earlier characters omitted from conversation" in prompt
    assert "old request 0" not in prompt


async def test_openai_compatible_provider_uses_bounded_context(monkeypatch) -> None:
    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "bounded"}}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb) -> None:
            return None

        async def post(self, _url: str, **kwargs):
            captured.update(kwargs["json"])
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    provider = OpenAICompatibleProvider("http://provider.invalid", "secret", "model")
    context = AssistantContext(
        open_file="main.tex",
        open_file_content="source",
    )

    response = await provider.chat(
        [AssistantMessage(role="user", content="x" * 2_000_000 + "LATEST")],
        context,
    )

    assert response.message == "bounded"
    provider_prompt = captured["messages"][-1]["content"]
    assert len(provider_prompt) <= CODEX_MAX_PROMPT_CHARS
    assert provider_prompt.endswith("LATEST")


class CapturingProvider(AssistantProvider):
    def __init__(self) -> None:
        self.context: AssistantContext | None = None

    async def chat(
        self, messages: list[AssistantMessage], context: AssistantContext
    ) -> AssistantResponse:
        self.context = context
        return AssistantResponse(message=f"Received {messages[-1].content}")


def test_assistant_receives_complete_context_without_credentials(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("manuscript", encoding="utf-8")
    provider = CapturingProvider()
    settings = Settings(
        CLOVERLEAF_WORKSPACE=workspace,
        AI_PROVIDER="openai-compatible",
        AI_API_KEY="server-secret",
    )
    app = create_app(settings, provider)
    body = {
        "messages": [{"role": "user", "content": "Explain this"}],
        "context": {
            "open_file": "main.tex",
            "open_file_content": "manuscript",
            "selected_text": "script",
            "diagnostics": [
                {"severity": "error", "message": "Undefined control sequence", "line": 4}
            ],
        },
    }

    with TestClient(app) as client:
        response = client.post("/api/assistant/chat", json=body)

    assert response.status_code == 200
    assert provider.context is not None
    assert provider.context.open_file == "main.tex"
    assert provider.context.selected_text == "script"
    assert provider.context.diagnostics[0].line == 4
    assert "server-secret" not in response.text
