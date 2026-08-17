import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cloverleaf.assistant import (
    ASSISTANT_DEVELOPER_INSTRUCTIONS,
    ASSISTANT_OUTPUT_SCHEMA,
    CODEX_MAX_PROMPT_CHARS,
    AssistantProvider,
    CodexProvider,
    OpenAICompatibleProvider,
    _consume_codex_turn,
    build_codex_prompt,
    codex_event_progress,
    parse_response,
)
from cloverleaf.config import Settings
from cloverleaf.main import create_app
from cloverleaf.models import (
    AssistantContext,
    AssistantMessage,
    AssistantResponse,
    CompileStatus,
    Diagnostic,
)


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


def test_parse_structured_edit_response() -> None:
    response = parse_response(
        json.dumps(
            {
                "message": "Reworked the opening for review.",
                "proposed_edits": [
                    {
                        "path": "main.tex",
                        "summary": "Replace the manuscript opening",
                        "replacements": [
                            {
                                "old_text": "old opening",
                                "new_text": "\\documentclass{article}\n",
                            }
                        ],
                    }
                ],
            }
        )
    )

    assert response.message == "Reworked the opening for review."
    replacement = response.proposed_edits[0].replacements[0]
    assert replacement.old_text == "old opening"
    assert replacement.new_text == "\\documentclass{article}\n"


def test_developer_instructions_make_latex_edits_the_default_action() -> None:
    instructions = ASSISTANT_DEVELOPER_INSTRUCTIONS.lower()

    assert "main latex document" in instructions
    assert "compiles successfully" in instructions
    assert "act instead of merely advising" in instructions
    assert "do not put suggested wording" in instructions
    assert "smallest practical sequence" in instructions
    assert "match exactly once" in instructions
    assert "proposed_edits" in instructions
    assert ASSISTANT_OUTPUT_SCHEMA["required"] == ["message", "proposed_edits"]
    edit_schema = ASSISTANT_OUTPUT_SCHEMA["properties"]["proposed_edits"]["items"]
    assert edit_schema["required"] == ["path", "summary", "replacements"]


def test_malformed_edit_block_remains_visible() -> None:
    content = "Explanation\n```cloverleaf-edits\nnot json\n```"
    response = parse_response(content)
    assert response.message == content
    assert response.proposed_edits == []


def test_codex_events_report_safe_runtime_phases() -> None:
    reasoning = SimpleNamespace(
        method="item/started",
        payload=SimpleNamespace(item=SimpleNamespace(root=SimpleNamespace(type="reasoning"))),
    )
    command = SimpleNamespace(
        method="item/started",
        payload=SimpleNamespace(
            item=SimpleNamespace(
                root=SimpleNamespace(type="commandExecution", command="cat secret.env")
            )
        ),
    )

    assert codex_event_progress(reasoning) == (
        "analyzing",
        "Analyzing the manuscript and project context…",
    )
    assert codex_event_progress(command) == (
        "inspecting",
        "Inspecting project files in the read-only workspace…",
    )
    assert "secret" not in codex_event_progress(command)[1]


async def test_codex_turn_stream_collects_response_and_progress() -> None:
    final_item = SimpleNamespace(
        root=SimpleNamespace(
            type="agentMessage",
            text="A healthy streamed response",
            phase=SimpleNamespace(value="final_answer"),
        )
    )
    events = [
        SimpleNamespace(method="turn/started", payload=SimpleNamespace()),
        SimpleNamespace(
            method="item/started",
            payload=SimpleNamespace(item=SimpleNamespace(root=SimpleNamespace(type="reasoning"))),
        ),
        SimpleNamespace(method="item/completed", payload=SimpleNamespace(item=final_item)),
        SimpleNamespace(
            method="turn/completed",
            payload=SimpleNamespace(
                turn=SimpleNamespace(status=SimpleNamespace(value="completed"), error=None)
            ),
        ),
    ]

    class FakeTurn:
        async def stream(self):
            for event in events:
                yield event

    progress: list[tuple[str, str]] = []
    response = await _consume_codex_turn(FakeTurn(), lambda *update: progress.append(update))

    assert response == "A healthy streamed response"
    assert [phase for phase, _message in progress] == ["working", "analyzing", "complete"]


def test_codex_prompt_sends_open_file_without_project_tree() -> None:
    context = AssistantContext(
        main_file="paper.tex",
        open_file="main.tex",
        open_file_content="A" * 400_000 + "END OF MANUSCRIPT",
        selected_text="important selection",
        compile_state="error",
        compile_log="Latexmk failed after an undefined control sequence",
    )

    prompt = build_codex_prompt(
        [AssistantMessage(role="user", content="Explain the manuscript")],
        context,
        workspace_root="/projects/word-salad",
    )

    assert len(prompt) <= CODEX_MAX_PROMPT_CHARS
    assert "main.tex" in prompt
    assert "important selection" in prompt
    assert "END OF MANUSCRIPT" in prompt
    assert "Explain the manuscript" in prompt
    assert "PROJECT TREE" not in prompt
    assert "inspect the current workspace" in prompt
    assert "WORKSPACE ROOT: /projects/word-salad" in prompt
    assert "COMPILATION ROOT: paper.tex" in prompt
    assert "COMPILE STATE: error" in prompt
    assert "undefined control sequence" in prompt


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
    assert "Act instead of merely advising" in captured["messages"][0]["content"]


async def test_codex_provider_uses_developer_preamble_and_structured_output(
    monkeypatch,
) -> None:
    import openai_codex

    captured: dict = {}
    final_item = SimpleNamespace(
        root=SimpleNamespace(
            type="agentMessage",
            text=json.dumps(
                {
                    "message": "Prepared the requested manuscript change.",
                    "proposed_edits": [
                        {
                            "path": "main.tex",
                            "summary": "Update the manuscript",
                            "replacements": [
                                {"old_text": "old", "new_text": "new"}
                            ],
                        }
                    ],
                }
            ),
            phase=SimpleNamespace(value="final_answer"),
        )
    )

    class FakeTurn:
        async def stream(self):
            yield SimpleNamespace(
                method="item/completed", payload=SimpleNamespace(item=final_item)
            )
            yield SimpleNamespace(
                method="turn/completed",
                payload=SimpleNamespace(
                    turn=SimpleNamespace(
                        status=SimpleNamespace(value="completed"), error=None
                    )
                ),
            )

    class FakeThread:
        async def turn(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["turn"] = kwargs
            return FakeTurn()

    class FakeCodex:
        def __init__(self, config):
            captured["config"] = config

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            return None

        async def thread_start(self, **kwargs):
            captured["thread"] = kwargs
            return FakeThread()

    monkeypatch.setattr(openai_codex, "AsyncCodex", FakeCodex)
    provider = CodexProvider("gpt-test", "/projects/manuscript", "/usr/bin/codex")

    response = await provider.chat(
        [AssistantMessage(role="user", content="Rewrite the opening")],
        AssistantContext(main_file="main.tex", open_file="main.tex"),
    )

    assert response.proposed_edits[0].path == "main.tex"
    assert captured["thread"]["developer_instructions"] == ASSISTANT_DEVELOPER_INSTRUCTIONS
    assert captured["thread"]["ephemeral"] is True
    assert captured["thread"]["cwd"] == "/projects/manuscript"
    assert captured["turn"]["output_schema"] == ASSISTANT_OUTPUT_SCHEMA


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
            "main_file": "wrong.tex",
            "open_file": "main.tex",
            "open_file_content": "manuscript",
            "selected_text": "script",
            "compile_state": "error",
            "compile_log": "compiler detail",
            "diagnostics": [
                {"severity": "error", "message": "Undefined control sequence", "line": 4}
            ],
        },
    }

    with TestClient(app) as client:
        client.app.state.compiler.status = CompileStatus(
            state="error",
            diagnostics=[
                Diagnostic(
                    severity="error",
                    message="Authoritative compiler diagnostic",
                    file="main.tex",
                    line=7,
                )
            ],
            log_tail="authoritative compiler detail",
        )
        response = client.post("/api/assistant/chat", json=body)

    assert response.status_code == 200
    assert provider.context is not None
    assert provider.context.main_file == "main.tex"
    assert provider.context.open_file == "main.tex"
    assert provider.context.selected_text == "script"
    assert provider.context.diagnostics[0].line == 7
    assert provider.context.diagnostics[0].message == "Authoritative compiler diagnostic"
    assert provider.context.compile_state == "error"
    assert provider.context.compile_log == "authoritative compiler detail"
    assert "server-secret" not in response.text


class EditProvider(AssistantProvider):
    def __init__(self, path: str = "main.tex") -> None:
        self.path = path

    async def chat(
        self, messages: list[AssistantMessage], context: AssistantContext
    ) -> AssistantResponse:
        return AssistantResponse(
            message="Prepared a concrete edit.",
            proposed_edits=[
                {
                    "path": self.path,
                    "summary": "Revise the manuscript",
                    "replacements": [
                        {
                            "old_text": "original manuscript",
                            "new_text": "revised manuscript",
                        }
                    ],
                }
            ],
        )


def test_assistant_edits_are_versioned_before_review(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manuscript = workspace / "main.tex"
    manuscript.write_text("original manuscript", encoding="utf-8")
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")

    with TestClient(create_app(settings, EditProvider())) as client:
        response = client.post(
            "/api/assistant/chat",
            json={"messages": [{"role": "user", "content": "Revise it"}], "context": {}},
        )
        edit = response.json()["proposed_edits"][0]
        manuscript.write_text("newer human edit", encoding="utf-8")
        stale_apply = client.put(
            "/api/files/main.tex",
            json={
                "path": edit["path"],
                "content": edit["content"],
                "version": edit["version"],
            },
        )

    assert response.status_code == 200
    assert edit["is_new"] is False
    assert edit["version"]
    assert edit["content"] == "revised manuscript"
    assert edit["replacements"] == [
        {"old_text": "original manuscript", "new_text": "revised manuscript"}
    ]
    assert stale_apply.status_code == 409
    assert manuscript.read_text(encoding="utf-8") == "newer human edit"


def test_assistant_rejects_unsafe_edit_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("manuscript", encoding="utf-8")
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")

    with TestClient(create_app(settings, EditProvider("../outside.tex"))) as client:
        response = client.post(
            "/api/assistant/chat",
            json={"messages": [{"role": "user", "content": "Revise it"}], "context": {}},
        )

    assert response.status_code == 503
    assert "safe project file boundary" in response.json()["detail"]
    assert not (tmp_path / "outside.tex").exists()


class ReplacementProvider(AssistantProvider):
    def __init__(self, replacements, path: str = "main.tex") -> None:
        self.path = path
        self.replacements = replacements

    async def chat(
        self, messages: list[AssistantMessage], context: AssistantContext
    ) -> AssistantResponse:
        return AssistantResponse(
            message="Prepared compact edits.",
            proposed_edits=[
                {
                    "path": self.path,
                    "summary": "Apply exact replacements",
                    "replacements": self.replacements,
                }
            ],
        )


def test_compact_assistant_replacements_are_materialized_in_order(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("alpha beta gamma", encoding="utf-8")
    provider = ReplacementProvider(
        [
            {"old_text": "alpha", "new_text": "one"},
            {"old_text": "beta gamma", "new_text": "two"},
        ]
    )
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")

    with TestClient(create_app(settings, provider)) as client:
        response = client.post(
            "/api/assistant/chat",
            json={"messages": [{"role": "user", "content": "Revise it"}], "context": {}},
        )

    assert response.status_code == 200
    assert response.json()["proposed_edits"][0]["content"] == "one two"
    assert (workspace / "main.tex").read_text(encoding="utf-8") == "alpha beta gamma"


def test_compact_assistant_replacement_must_match_uniquely(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("repeat repeat", encoding="utf-8")
    provider = ReplacementProvider([{"old_text": "repeat", "new_text": "changed"}])
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")

    with TestClient(create_app(settings, provider)) as client:
        response = client.post(
            "/api/assistant/chat",
            json={"messages": [{"role": "user", "content": "Revise it"}], "context": {}},
        )

    assert response.status_code == 503
    assert "did not uniquely match" in response.json()["detail"]
    assert (workspace / "main.tex").read_text(encoding="utf-8") == "repeat repeat"


def test_compact_assistant_replacement_can_create_a_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("manuscript", encoding="utf-8")
    provider = ReplacementProvider(
        [{"old_text": "", "new_text": "new section\n"}],
        "sections/new.tex",
    )
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")

    with TestClient(create_app(settings, provider)) as client:
        response = client.post(
            "/api/assistant/chat",
            json={"messages": [{"role": "user", "content": "Add a section"}], "context": {}},
        )

    assert response.status_code == 200
    edit = response.json()["proposed_edits"][0]
    assert edit["content"] == "new section\n"
    assert edit["is_new"] is True
    assert not (workspace / "sections/new.tex").exists()


class SlowProgressProvider(AssistantProvider):
    async def chat(
        self, messages: list[AssistantMessage], context: AssistantContext
    ) -> AssistantResponse:
        return AssistantResponse(message="stream complete")

    async def chat_with_progress(self, messages, context, progress):
        import asyncio

        progress("connecting", "Connecting to fake Codex…")
        await asyncio.sleep(0.035)
        progress("responding", "Drafting the fake response…")
        return await self.chat(messages, context)


def test_assistant_progress_socket_reports_phases_and_heartbeats(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("manuscript", encoding="utf-8")
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")
    monkeypatch.setattr("cloverleaf.main.ASSISTANT_HEARTBEAT_SECONDS", 0.01)

    request_id = "12345678-1234-1234-1234-123456789abc"
    with TestClient(create_app(settings, SlowProgressProvider())) as client:
        with client.websocket_connect(f"/api/assistant/progress/{request_id}") as socket:
            events = [socket.receive_json()]
            response = client.post(
                "/api/assistant/chat",
                json={
                    "messages": [{"role": "user", "content": "Explain this"}],
                    "context": {},
                    "request_id": request_id,
                },
            )
            while True:
                try:
                    events.append(socket.receive_json())
                except WebSocketDisconnect:
                    break

    assert response.status_code == 200
    assert response.json()["message"] == "stream complete"
    assert events[0]["phase"] == "preparing"
    assert any(event["heartbeat"] for event in events)
    assert [event["phase"] for event in events if not event["heartbeat"]] == [
        "preparing",
        "connecting",
        "responding",
    ]


def test_assistant_request_id_is_bounded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("manuscript", encoding="utf-8")
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")

    with TestClient(create_app(settings, SlowProgressProvider())) as client:
        response = client.post(
            "/api/assistant/chat",
            json={
                "messages": [{"role": "user", "content": "Explain this"}],
                "context": {},
                "request_id": "../../not-a-request-id",
            },
        )

    assert response.status_code == 422
