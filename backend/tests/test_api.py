from pathlib import Path

from fastapi.testclient import TestClient

from cloverleaf.assistant import CodexProvider
from cloverleaf.config import Settings
from cloverleaf.main import create_app
from cloverleaf.models import AssistantContext, AssistantMessage, AssistantResponse


def make_client(tmp_path: Path) -> TestClient:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("hello", encoding="utf-8")
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")
    return TestClient(create_app(settings))


def test_file_api_lifecycle(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert client.get("/api/files/main.tex").json()["content"] == "hello"
        assert client.post(
            "/api/files", json={"path": "notes", "type": "directory"}
        ).status_code == 201
        assert client.post(
            "/api/files",
            json={"path": "notes/draft.tex", "type": "file", "content": "draft"},
        ).status_code == 201
        assert client.put(
            "/api/files/notes/draft.tex",
            json={"path": "notes/draft.tex", "content": "revised"},
        ).status_code == 200
        assert client.patch(
            "/api/files",
            json={"path": "notes/draft.tex", "new_path": "notes/final.tex"},
        ).status_code == 200
        assert client.delete("/api/files/notes/final.tex").status_code == 204
        assert client.delete("/api/files/notes").status_code == 204


def test_file_api_rejects_stale_editor_write(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        opened = client.get("/api/files/main.tex").json()
        (tmp_path / "workspace" / "main.tex").write_text("external", encoding="utf-8")
        response = client.put(
            "/api/files/main.tex",
            json={**opened, "content": "stale editor"},
        )

        assert response.status_code == 409
        assert "changed on disk" in response.json()["detail"]
        assert (tmp_path / "workspace" / "main.tex").read_text(encoding="utf-8") == "external"


def test_encoded_and_backslash_traversal_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "secret.tex").write_text("secret", encoding="utf-8")
    with make_client(tmp_path) as client:
        paths = [
            "/api/files/%252e%252e/secret.tex",
            "/api/files/%2e%2e%5csecret.tex",
            "/api/files/%252fetc%252fpasswd",
        ]
        for path in paths:
            response = client.get(path)
            assert response.status_code in {400, 404}
            assert "secret" not in response.text


def test_symlink_escape_is_rejected_by_api(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.tex").write_text("secret", encoding="utf-8")
    try:
        (workspace / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        return
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/files/linked/secret.tex")

    assert response.status_code == 400
    assert "secret" not in response.text


def test_health_and_missing_pdf_are_clear(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert client.get("/api/health").json() == {"ok": True}
        assert client.get("/api/runtime").json() == {
            "managed": False,
            "log_available": False,
            "shutdown_available": False,
        }
        shutdown = client.post("/api/runtime/shutdown")
        assert shutdown.status_code == 409
        assert "launcher" in shutdown.json()["detail"]
        response = client.get("/api/pdf")
        assert response.status_code == 404
        assert response.json()["detail"] == "No successful PDF build is available yet"


def test_launcher_runtime_logs_and_shutdown_signal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("hello", encoding="utf-8")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    log_path = runtime_dir / "server.log"
    log_path.write_text("[backend:stderr] server ready\n", encoding="utf-8")
    shutdown_path = runtime_dir / "shutdown"
    settings = Settings(
        CLOVERLEAF_WORKSPACE=workspace,
        AI_PROVIDER="disabled",
        CLOVERLEAF_RUNTIME_LOG=str(log_path),
        CLOVERLEAF_RUNTIME_SHUTDOWN=str(shutdown_path),
    )

    with TestClient(create_app(settings)) as client:
        assert client.get("/api/runtime").json() == {
            "managed": True,
            "log_available": True,
            "shutdown_available": True,
        }
        with client.websocket_connect("/api/runtime/logs") as socket:
            assert "server ready" in socket.receive_text()
        response = client.post("/api/runtime/shutdown")

    assert response.status_code == 202
    assert response.json()["message"] == "Server shutdown requested"
    assert shutdown_path.read_text(encoding="utf-8") == "shutdown requested\n"


def test_project_can_be_loaded_at_runtime(tmp_path: Path) -> None:
    other = tmp_path / "other-project"
    (other / "chapters").mkdir(parents=True)
    (other / "paper.tex").write_text("new manuscript", encoding="utf-8")
    (other / "chapters" / "one.tex").write_text("chapter", encoding="utf-8")

    with make_client(tmp_path) as client:
        initial = client.get("/api/project").json()
        assert initial["main_file"] == "main.tex"

        response = client.post(
            "/api/project/load",
            json={"workspace": str(other), "main_file": "paper.tex"},
        )

        assert response.status_code == 200
        assert response.json() == {
            "workspace": str(other.resolve()),
            "name": "other-project",
            "main_file": "paper.tex",
        }
        assert client.get("/api/files/paper.tex").json()["content"] == "new manuscript"
        assert [node["name"] for node in client.get("/api/project/tree").json()] == [
            "chapters",
            "paper.tex",
        ]
        assert client.app.state.compiler.workspace == other.resolve()
        assert client.app.state.compiler.main_file == "paper.tex"

    state_path = tmp_path / ".cloverleaf-project.json"
    assert state_path.exists()
    settings = Settings(
        CLOVERLEAF_WORKSPACE=tmp_path / "workspace",
        AI_PROVIDER="disabled",
    )
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get("/api/project").json() == {
            "workspace": str(other.resolve()),
            "name": "other-project",
            "main_file": "paper.tex",
        }


def test_project_folder_browser_lists_safe_choices(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "visible").mkdir()
    (workspace / ".hidden").mkdir()
    (workspace / "notes.txt").write_text("notes", encoding="utf-8")
    try:
        (workspace / "linked").symlink_to(workspace / "visible", target_is_directory=True)
    except OSError:
        pass

    with client:
        response = client.get("/api/project/directories", params={"path": str(workspace)})

        assert response.status_code == 200
        listing = response.json()
        assert listing["path"] == str(workspace.resolve())
        assert [entry["name"] for entry in listing["directories"]] == ["visible"]
        assert listing["tex_files"] == ["main.tex"]
        assert listing["parent"] == str(tmp_path.resolve())
        assert Path(listing["home"]).is_absolute()
        assert Path(listing["root"]).is_absolute()
        assert client.get(
            "/api/project/directories",
            params={"path": "relative"},
        ).status_code == 400
        assert client.get(
            "/api/project/directories",
            params={"path": str(workspace / "main.tex")},
        ).status_code == 400


def test_loading_empty_project_creates_main_tex(tmp_path: Path) -> None:
    empty = tmp_path / "empty-project"
    empty.mkdir()

    with make_client(tmp_path) as client:
        response = client.post(
            "/api/project/load",
            json={"workspace": str(empty), "main_file": "main.tex"},
        )

        assert response.status_code == 200
        assert response.json()["main_file"] == "main.tex"
        content = (empty / "main.tex").read_text(encoding="utf-8")
        assert "\\documentclass{article}" in content
        assert "Start writing here." in content
        assert client.get("/api/files/main.tex").json()["content"] == content


def test_invalid_project_load_does_not_replace_current_project(tmp_path: Path) -> None:
    outside = tmp_path / "outside.tex"
    outside.write_text("outside", encoding="utf-8")

    with make_client(tmp_path) as client:
        original = client.get("/api/project").json()
        attempts = [
            ({"workspace": "relative", "main_file": "main.tex"}, 400),
            ({"workspace": str(tmp_path / "missing"), "main_file": "main.tex"}, 404),
            ({"workspace": str(tmp_path), "main_file": "../outside.tex"}, 400),
            ({"workspace": str(tmp_path), "main_file": "outside.txt"}, 400),
            ({"workspace": str(tmp_path), "main_file": "missing.tex"}, 404),
        ]
        for payload, expected_status in attempts:
            assert client.post("/api/project/load", json=payload).status_code == expected_status
            assert client.get("/api/project").json() == original
            assert client.get("/api/files/main.tex").json()["content"] == "hello"


def test_loading_project_rebinds_codex_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("first", encoding="utf-8")
    other = tmp_path / "second"
    other.mkdir()
    (other / "paper.tex").write_text("second", encoding="utf-8")
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="codex")

    with TestClient(create_app(settings)) as client:
        assert isinstance(client.app.state.assistant, CodexProvider)
        response = client.post(
            "/api/project/load",
            json={"workspace": str(other), "main_file": "paper.tex"},
        )

        assert response.status_code == 200
        assert isinstance(client.app.state.assistant, CodexProvider)
        assert client.app.state.assistant.workspace == str(other.resolve())


class RecordingCodexProvider(CodexProvider):
    async def chat(
        self,
        messages: list[AssistantMessage],
        context: AssistantContext,
    ) -> AssistantResponse:
        return AssistantResponse(message=f"{self.workspace}: {messages[-1].content}")

    def for_workspace(self, workspace: Path) -> "RecordingCodexProvider":
        return RecordingCodexProvider(self.model, str(workspace), self.codex_bin)


def test_assistant_turn_defensively_uses_active_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("source", encoding="utf-8")
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")
    provider = RecordingCodexProvider("model", "/stale")

    with TestClient(create_app(settings, provider)) as client:
        client.app.state.assistant = RecordingCodexProvider("model", "/stale")
        response = client.post(
            "/api/assistant/chat",
            json={
                "messages": [{"role": "user", "content": "where am I?"}],
                "context": {},
            },
        )

        assert response.status_code == 200
        assert response.json()["message"] == f"{workspace.resolve()}: where am I?"
        assert client.app.state.assistant.workspace == str(workspace.resolve())
