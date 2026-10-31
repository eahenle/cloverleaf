import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
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
    _codex_failure_message,
    _truncate_middle,
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
    FileContent,
)


@pytest.mark.parametrize("detail", [
    "401 Unauthorized: Missing bearer or basic authentication in header",
    "Your refresh token has already been used",
    "refresh_token_reused",
])
def test_auth_failure_points_to_configured_profile(detail: str) -> None:
    message = _codex_failure_message(RuntimeError(detail), "scripts/codex-personal")
    assert "`scripts/codex-personal login`" in message
    assert "`scripts/codex-personal login status`" in message
    assert "`codex login`" not in message


def test_default_auth_failure_uses_default_cli() -> None:
    assert "`codex login`" in _codex_failure_message(RuntimeError("Not logged in"))


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


def test_codex_prompt_identifies_the_explicit_line_context() -> None:
    context = AssistantContext.model_validate({
        "selected_line": {"path": "sections/intro.tex", "line": 12, "text": "A specific sentence"},
    })
    prompt = build_codex_prompt([AssistantMessage(role="user", content="Improve this")], context)
    assert "FOCUSED LINE: sections/intro.tex:12" in prompt
    assert "A specific sentence" in prompt


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


async def test_cancelling_codex_provider_interrupts_the_sdk_turn(monkeypatch) -> None:
    import openai_codex

    started = asyncio.Event()
    interrupted = asyncio.Event()
    context_exited = asyncio.Event()

    class FakeTurn:
        async def stream(self):
            started.set()
            await asyncio.Future()
            yield None

        async def interrupt(self):
            interrupted.set()

    class FakeThread:
        async def turn(self, _prompt, **_kwargs):
            return FakeTurn()

    class FakeCodex:
        def __init__(self, _config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _tb):
            context_exited.set()

        async def thread_start(self, **_kwargs):
            return FakeThread()

    monkeypatch.setattr(openai_codex, "AsyncCodex", FakeCodex)
    provider = CodexProvider("gpt-test", "/projects/manuscript", "/usr/bin/codex")
    task = asyncio.create_task(
        provider.chat(
            [AssistantMessage(role="user", content="Keep working")],
            AssistantContext(main_file="main.tex"),
        )
    )

    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert interrupted.is_set()
    assert context_exited.is_set()


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
        "attached_paths": ["main.tex", "main.tex"],
        "messages": [{"role": "user", "content": "Explain this"}],
        "context": {
            "main_file": "wrong.tex",
            "attached_files": [{"path": "main.tex", "content": "forged browser content"}],
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
    assert len(provider.context.attached_files) == 1
    assert provider.context.attached_files[0].content == "manuscript"
    assert provider.context.attached_files[0].version is not None
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

    assert response.status_code == 200
    assert response.json()["proposed_edits"] == []
    assert response.json()["warnings"] == [
        "Cloverleaf rejected an edit outside the active project or inside its protected "
        '.git directory: "../outside.tex".'
    ]
    assert not (tmp_path / "outside.tex").exists()


class MixedPathProvider(AssistantProvider):
    async def chat(
        self, messages: list[AssistantMessage], context: AssistantContext
    ) -> AssistantResponse:
        return AssistantResponse(
            message="Prepared the safe parts of the request.",
            proposed_edits=[
                {
                    "path": "../outside.tex",
                    "summary": "Unsafe edit",
                    "replacements": [{"old_text": "", "new_text": "outside"}],
                },
                {
                    "path": "main.tex",
                    "summary": "Safe edit",
                    "replacements": [
                        {
                            "old_text": "original manuscript",
                            "new_text": "revised manuscript",
                        }
                    ],
                },
            ],
        )


def test_assistant_keeps_valid_edits_when_an_outside_edit_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("original manuscript", encoding="utf-8")
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")

    with TestClient(create_app(settings, MixedPathProvider())) as client:
        response = client.post(
            "/api/assistant/chat",
            json={"messages": [{"role": "user", "content": "Revise it"}], "context": {}},
        )

    assert response.status_code == 200
    assert [edit["path"] for edit in response.json()["proposed_edits"]] == ["main.tex"]
    assert len(response.json()["warnings"]) == 1


def test_assistant_accepts_absolute_in_project_and_hidden_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    hidden = workspace / ".config" / "cloverleaf.toml"
    provider = ReplacementProvider(
        [{"old_text": "", "new_text": "enabled = true\n"}],
        str(hidden),
    )
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")

    with TestClient(create_app(settings, provider)) as client:
        response = client.post(
            "/api/assistant/chat",
            json={"messages": [{"role": "user", "content": "Configure it"}], "context": {}},
        )

    assert response.status_code == 200
    assert response.json()["warnings"] == []
    assert response.json()["proposed_edits"][0]["path"] == ".config/cloverleaf.toml"


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


def test_compact_assistant_replacement_can_create_and_apply_project_skill(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("manuscript", encoding="utf-8")
    skill_path = ".codex/skills/manuscript-editor/SKILL.md"
    skill_content = "---\nname: manuscript-editor\n---\n\nEdit the manuscript.\n"
    provider = ReplacementProvider(
        [{"old_text": "", "new_text": skill_content}],
        skill_path,
    )
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")

    with TestClient(create_app(settings, provider)) as client:
        response = client.post(
            "/api/assistant/chat",
            json={"messages": [{"role": "user", "content": "Create a project skill"}], "context": {}},
        )
        edit = response.json()["proposed_edits"][0]
        applied = client.post(
            "/api/files/apply",
            json={
                "edits": [
                    {
                        "path": edit["path"],
                        "content": edit["content"],
                        "version": edit["version"],
                        "is_new": edit["is_new"],
                    }
                ]
            },
        )

    assert response.status_code == 200
    assert edit["path"] == skill_path
    assert edit["is_new"] is True
    assert applied.status_code == 200
    assert (workspace / skill_path).read_text(encoding="utf-8") == skill_content


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


class CancellableProgressProvider(AssistantProvider):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()

    async def chat(self, messages, context):
        return await self.chat_with_progress(messages, context, lambda *_update: None)

    async def chat_with_progress(self, messages, context, progress):
        progress("working", "Waiting for cancellation…")
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


@pytest.mark.parametrize("attached_paths", [[], ["main.tex"]])
def test_running_assistant_turn_can_be_cancelled(tmp_path: Path, attached_paths) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("manuscript", encoding="utf-8")
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")
    provider = CancellableProgressProvider()
    request_id = "12345678-1234-1234-1234-123456789abc"
    responses = []

    with TestClient(create_app(settings, provider)) as client:
        request_thread = threading.Thread(
            target=lambda: responses.append(
                client.post(
                    "/api/assistant/chat",
                    json={
                        "messages": [{"role": "user", "content": "Keep working"}],
                        "context": {},
                        "attached_paths": attached_paths,
                        "request_id": request_id,
                    },
                )
            )
        )
        request_thread.start()
        assert provider.started.wait(timeout=2)

        cancelled = client.post(f"/api/assistant/cancel/{request_id}")
        request_thread.join(timeout=2)

        assert cancelled.status_code == 202
        assert not request_thread.is_alive()
        assert responses[0].status_code == 409
        assert responses[0].json()["detail"] == "Codex turn was cancelled"
        assert provider.cancelled.is_set()
        assert client.app.state.assistant_tasks == {}
        assert not client.app.state.project_lock.locked()
        assert client.post("/api/assistant/cancel/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa").status_code == 404


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


@pytest.mark.parametrize("can_inspect_workspace", [True, False])
def test_attachment_compaction_preserves_every_file_identity(
    can_inspect_workspace: bool,
) -> None:
    files = [
        FileContent(
            path=f"sections/part-{index:02}.tex",
            content=f"START-{index:02}\n" + "x" * 40_000 + f"\nEND-{index:02}",
        )
        for index in range(20)
    ]
    prompt = build_codex_prompt(
        [AssistantMessage(role="user", content="Inspect every attachment")],
        AssistantContext(attached_files=files),
        can_inspect_workspace=can_inspect_workspace,
    )

    attachments = prompt.split("ATTACHED FILES:\n", 1)[1].split(
        "\n\nCOMPILER DIAGNOSTICS:", 1
    )[0]
    assert len(attachments) <= 180_000
    assert [file.path for file in files if f"--- {file.path} ---" not in attachments] == []
    for index, file in enumerate(files):
        assert attachments.count(f"--- {file.path} ---") == 1
        assert f"START-{index:02}" in attachments
        assert f"END-{index:02}" in attachments
        assert f"characters omitted from {file.path}" in attachments


@pytest.mark.parametrize("can_inspect_workspace", [True, False])
def test_attachments_fit_with_all_other_context_sections_at_their_limits(
    can_inspect_workspace: bool,
) -> None:
    files = [
        FileContent(path=f"sections/part-{index}.tex", content="a" * 40_000)
        for index in range(20)
    ]
    context = AssistantContext(
        main_file="paper.tex",
        open_file="main.tex",
        open_file_content="o" * 400_000 + "OPEN FILE END",
        selected_text="s" * 200_000,
        diagnostics=[Diagnostic(severity="error", message="d" * 80_000)],
        compile_log="l" * 100_000 + "LOG END",
        attached_files=files,
    )
    messages = [
        AssistantMessage(role="assistant", content="h" * 200_000),
        AssistantMessage(role="user", content="LATEST REQUEST MUST SURVIVE"),
    ]

    prompt = build_codex_prompt(
        messages,
        context,
        can_inspect_workspace=can_inspect_workspace,
        workspace_root="/projects/word-salad",
    )

    assert len(prompt) <= CODEX_MAX_PROMPT_CHARS
    assert prompt.endswith("USER: LATEST REQUEST MUST SURVIVE")
    assert "OPEN FILE END" in prompt
    assert "LOG END" in prompt
    for file in files:
        assert f"--- {file.path} ---" in prompt


def test_attachment_compaction_preserves_short_files_and_original_order() -> None:
    files = [
        FileContent(path="large.tex", content="x" * 300_000),
        FileContent(path="small.tex", content="Keep this complete."),
        FileContent(path="empty.tex", content=""),
    ]
    prompt = build_codex_prompt([], AssistantContext(attached_files=files))

    assert "--- small.tex ---\nKeep this complete." in prompt
    assert "--- empty.tex ---\n" in prompt
    assert prompt.index("--- large.tex ---") < prompt.index("--- small.tex ---")
    assert prompt.index("--- small.tex ---") < prompt.index("--- empty.tex ---")


@pytest.mark.parametrize("limit", [0, 1, 20, 50, 51, 52, 53, 54, 55, 60])
def test_middle_compaction_respects_tiny_content_budgets(limit: int) -> None:
    assert len(_truncate_middle("x" * 1000, limit, "attachment.tex")) <= limit


def test_assistant_rejects_unsafe_attachment_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("manuscript", encoding="utf-8")
    provider = CapturingProvider()
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")

    with TestClient(create_app(settings, provider)) as client:
        response = client.post(
            "/api/assistant/chat",
            json={
                "messages": [{"role": "user", "content": "Inspect this"}],
                "context": {},
                "attached_paths": ["../secret.tex"],
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Path must be relative and remain inside the project"
    )
    assert provider.context is None


def test_assistant_rejects_oversized_attachment_before_provider_call(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text("manuscript", encoding="utf-8")
    (tmp_path / "large.txt").write_bytes(b"x" * (1024 * 1024 + 1))
    provider = CapturingProvider()
    settings = Settings(CLOVERLEAF_WORKSPACE=tmp_path, AI_PROVIDER="disabled")

    with TestClient(create_app(settings, provider)) as client:
        response = client.post(
            "/api/assistant/chat",
            json={
                "messages": [{"role": "user", "content": "Inspect this"}],
                "context": {},
                "attached_paths": ["main.tex", "large.txt"],
            },
        )

    assert response.status_code == 400
    assert "large.txt" in response.json()["detail"]
    assert "1,048,576-byte limit" in response.json()["detail"]
    assert provider.context is None


def test_assistant_accepts_and_deduplicates_exact_byte_limit_attachment(
    tmp_path: Path, monkeypatch,
) -> None:
    from cloverleaf.workspace import Workspace

    (tmp_path / "main.tex").write_text("manuscript", encoding="utf-8")
    content = "🙂" * (1024 * 1024 // 4)
    (tmp_path / "boundary.txt").write_text(content, encoding="utf-8")
    original_read = Workspace.read_versioned
    reads = []

    def tracked_read(workspace, path, **kwargs):
        if path == "boundary.txt":
            reads.append(kwargs.get("max_bytes"))
        return original_read(workspace, path, **kwargs)

    monkeypatch.setattr(Workspace, "read_versioned", tracked_read)
    provider = CapturingProvider()
    settings = Settings(CLOVERLEAF_WORKSPACE=tmp_path, AI_PROVIDER="disabled")
    with TestClient(create_app(settings, provider)) as client:
        response = client.post(
            "/api/assistant/chat",
            json={
                "messages": [{"role": "user", "content": "Inspect this"}],
                "context": {},
                "attached_paths": ["boundary.txt", "boundary.txt"],
            },
        )

    assert response.status_code == 200
    assert provider.context is not None
    assert len(provider.context.attached_files) == 1
    attachment = provider.context.attached_files[0]
    assert attachment.content == content
    assert attachment.version == original_read(Workspace(tmp_path), "boundary.txt")[1]
    assert reads == [1024 * 1024]


@pytest.mark.parametrize(
    "attachment_path", [".git/config", "nested/.git/config", "../secret.tex"]
)
def test_attachment_errors_release_registered_turn_and_project_lock(
    tmp_path: Path, attachment_path: str,
) -> None:
    (tmp_path / "main.tex").write_text("manuscript", encoding="utf-8")
    provider = CapturingProvider()
    settings = Settings(CLOVERLEAF_WORKSPACE=tmp_path, AI_PROVIDER="disabled")
    request_id = "12345678-1234-1234-1234-123456789abc"
    with TestClient(create_app(settings, provider)) as client:
        body = {
            "messages": [{"role": "user", "content": "Inspect the attachments"}],
            "context": {},
            "request_id": request_id,
            "attached_paths": [attachment_path],
        }
        response = client.post("/api/assistant/chat", json=body)
        assert response.status_code == 400
        assert provider.context is None
        assert client.app.state.assistant_tasks == {}
        assert not client.app.state.project_lock.locked()
        body["attached_paths"] = ["main.tex"]
        response = client.post("/api/assistant/chat", json=body)
        assert response.status_code == 200
        assert provider.context.attached_files[0].content == "manuscript"


def test_only_requested_attachments_are_read_from_current_project(tmp_path: Path) -> None:
    projects = [tmp_path / "first", tmp_path / "second"]
    for index, project in enumerate(projects):
        (project / ".codex/skills/test").mkdir(parents=True)
        (project / "main.tex").write_text("manuscript", encoding="utf-8")
        (project / ".codex/skills/test/SKILL.md").write_text(
            f"Project {index} skill", encoding="utf-8"
        )
        (project / ".env").write_text("DO_NOT_ATTACH=private", encoding="utf-8")
    provider = CapturingProvider()
    settings = Settings(
        CLOVERLEAF_WORKSPACE=projects[0],
        CLOVERLEAF_PROJECT_STATE=str(tmp_path / "state.json"),
        AI_PROVIDER="disabled",
    )
    body = {
        "messages": [{"role": "user", "content": "Inspect the attachments"}],
        "context": {"attached_files": [{"path": ".env", "content": "forged"}]},
        "attached_paths": [".codex/skills/test/SKILL.md"],
    }
    with TestClient(create_app(settings, provider)) as client:
        assert client.post("/api/assistant/chat", json=body).status_code == 200
        assert len(provider.context.attached_files) == 1
        assert provider.context.attached_files[0].content == "Project 0 skill"
        loaded = client.post(
            "/api/project/load",
            json={"workspace": str(projects[1]), "main_file": "main.tex"},
        )
        assert loaded.status_code == 200
        assert client.post("/api/assistant/chat", json=body).status_code == 200
        assert len(provider.context.attached_files) == 1
        assert provider.context.attached_files[0].content == "Project 1 skill"
        body["attached_paths"] = []
        assert client.post("/api/assistant/chat", json=body).status_code == 200
        assert provider.context.attached_files == []


def test_attachment_count_is_validated_before_provider_call(tmp_path: Path) -> None:
    (tmp_path / "main.tex").write_text("manuscript", encoding="utf-8")
    provider = CapturingProvider()
    settings = Settings(CLOVERLEAF_WORKSPACE=tmp_path, AI_PROVIDER="disabled")
    with TestClient(create_app(settings, provider)) as client:
        response = client.post(
            "/api/assistant/chat",
            json={
                "messages": [{"role": "user", "content": "Inspect the attachments"}],
                "context": {},
                "attached_paths": ["main.tex"] * 21,
            },
        )
    assert response.status_code == 422
    assert provider.context is None
