import json
from pathlib import Path

from fastapi.testclient import TestClient

from cloverleaf.assistant import AssistantProvider, parse_response
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
            "project_tree": [{"name": "main.tex", "path": "main.tex", "type": "file"}],
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
