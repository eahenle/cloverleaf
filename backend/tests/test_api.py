import asyncio
import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from cloverleaf.assistant import CodexProvider
from cloverleaf.config import Settings
from cloverleaf.main import create_app
from cloverleaf.models import AssistantContext, AssistantMessage, AssistantResponse


def make_client(tmp_path: Path, *, raise_server_exceptions: bool = True) -> TestClient:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("hello", encoding="utf-8")
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")
    return TestClient(create_app(settings), raise_server_exceptions=raise_server_exceptions)


def git_output(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


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


def test_file_api_writes_create_local_git_checkpoints(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    with make_client(tmp_path) as client:
        initial_count = int(git_output(workspace, "rev-list", "--count", "HEAD"))
        opened = client.get("/api/files/main.tex").json()

        response = client.put(
            "/api/files/main.tex",
            json={**opened, "content": "checkpointed manuscript"},
        )

        assert response.status_code == 200
        repository = client.get("/api/project/git")
        assert repository.status_code == 200
        assert repository.json()["initialized"] is True
        assert repository.json()["head"]
        assert repository.json()["last_error"] is None
        assert int(git_output(workspace, "rev-list", "--count", "HEAD")) == initial_count + 1
        assert git_output(workspace, "log", "-1", "--pretty=%s") == "Update main.tex"
        assert git_output(workspace, "show", "HEAD:main.tex") == "checkpointed manuscript"


def test_codex_history_is_project_local_persistent_and_not_checkpointed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("hello", encoding="utf-8")
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")

    with TestClient(create_app(settings)) as client:
        empty = client.get("/api/project/codex-history")
        assert empty.status_code == 200
        assert empty.json() == {
            "version": 1,
            "workspace": str(workspace.resolve()),
            "messages": [],
            "dismissed_edit_keys": [],
        }
        payload = {
            "version": 1,
            "workspace": str(workspace.resolve()),
            "messages": [
                {"role": "user", "content": "Revise the opening"},
                {
                    "role": "assistant",
                    "content": "Prepared one edit.",
                    "edits": [
                        {
                            "path": "main.tex",
                            "summary": "Revise the opening",
                            "replacements": [{"old_text": "hello", "new_text": "revised"}],
                            "content": "revised",
                            "version": "source-version",
                            "is_new": False,
                        }
                    ],
                },
            ],
            "dismissed_edit_keys": ["1-0"],
        }
        saved = client.put("/api/project/codex-history", json=payload)
        assert saved.status_code == 200
        canonical_history = saved.json()
        assert client.get("/api/project/codex-history").json() == canonical_history
        assert ".cloverleaf" not in {
            node["name"] for node in client.get("/api/project/tree").json()
        }
        assert git_output(workspace, "status", "--short") == ""
        wrong_project = client.put(
            "/api/project/codex-history",
            json={**payload, "workspace": str(tmp_path / "other")},
        )
        assert wrong_project.status_code == 409

    with TestClient(create_app(settings)) as restarted_client:
        restored = restarted_client.get("/api/project/codex-history")

    assert restored.status_code == 200
    assert restored.json() == canonical_history


def test_codex_history_rejects_a_state_directory_that_escapes_the_project(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "main.tex").write_text("hello", encoding="utf-8")
    try:
        (workspace / ".cloverleaf").symlink_to(outside, target_is_directory=True)
    except OSError:
        return
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")

    with TestClient(create_app(settings)) as client:
        empty = client.get("/api/project/codex-history")
        saved = client.put(
            "/api/project/codex-history",
            json={
                "version": 1,
                "workspace": str(workspace.resolve()),
                "messages": [{"role": "user", "content": "do not escape"}],
                "dismissed_edit_keys": [],
            },
        )

    assert empty.status_code == 200
    assert empty.json()["messages"] == []
    assert saved.status_code == 503
    assert not (outside / "codex-history.json").exists()


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
            "restart_available": False,
        }
        shutdown = client.post("/api/runtime/shutdown")
        assert shutdown.status_code == 409
        assert "launcher" in shutdown.json()["detail"]
        restart = client.post("/api/runtime/restart")
        assert restart.status_code == 409
        assert "launcher" in restart.json()["detail"]
        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["overall"] in {"healthy", "degraded"}
        assert {service["id"] for service in status.json()["services"]} == {
            "backend",
            "project",
            "git",
            "compiler",
            "assistant",
            "launcher",
        }
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
    restart_path = runtime_dir / "restart"
    settings = Settings(
        CLOVERLEAF_WORKSPACE=workspace,
        AI_PROVIDER="disabled",
        CLOVERLEAF_RUNTIME_LOG=str(log_path),
        CLOVERLEAF_RUNTIME_SHUTDOWN=str(shutdown_path),
        CLOVERLEAF_RUNTIME_RESTART=str(restart_path),
    )

    with TestClient(create_app(settings)) as client:
        assert client.get("/api/runtime").json() == {
            "managed": True,
            "log_available": True,
            "shutdown_available": True,
            "restart_available": True,
        }
        with client.websocket_connect("/api/runtime/logs") as socket:
            assert "server ready" in socket.receive_text()
        restart_response = client.post("/api/runtime/restart")
        response = client.post("/api/runtime/shutdown")

    assert restart_response.status_code == 202
    assert restart_response.json()["message"] == "Server restart requested"
    assert restart_path.read_text(encoding="utf-8") == "restart requested\n"
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


def test_compilation_root_can_change_within_active_project(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    views = workspace / "views"
    views.mkdir(parents=True)
    (workspace / "main.tex").write_text("main", encoding="utf-8")
    (views / "summary.tex").write_text("summary", encoding="utf-8")
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")

    with TestClient(create_app(settings)) as client:
        original_compiler = client.app.state.compiler
        response = client.post(
            "/api/project/compilation-root",
            json={"main_file": "views/summary.tex"},
        )

        assert response.status_code == 200
        assert response.json() == {
            "workspace": str(workspace.resolve()),
            "name": "workspace",
            "main_file": "views/summary.tex",
        }
        assert client.app.state.workspace.root == workspace.resolve()
        assert client.app.state.compiler is not original_compiler
        assert client.app.state.compiler.workspace == workspace.resolve()
        assert client.app.state.compiler.main_file == "views/summary.tex"

    persisted = json.loads((tmp_path / ".cloverleaf-project.json").read_text(encoding="utf-8"))
    assert persisted == {
        "workspace": str(workspace.resolve()),
        "main_file": "views/summary.tex",
    }
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get("/api/project").json()["main_file"] == "views/summary.tex"


def test_invalid_compilation_root_does_not_replace_current_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("main", encoding="utf-8")
    (workspace / "notes.txt").write_text("notes", encoding="utf-8")
    (workspace / "folder").mkdir()
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="disabled")

    with TestClient(create_app(settings)) as client:
        original_compiler = client.app.state.compiler
        attempts = [
            ("../outside.tex", 400),
            ("notes.txt", 400),
            ("missing.tex", 404),
            ("folder", 400),
        ]
        for main_file, expected_status in attempts:
            response = client.post(
                "/api/project/compilation-root",
                json={"main_file": main_file},
            )
            assert response.status_code == expected_status
            assert client.get("/api/project").json()["main_file"] == "main.tex"
            assert client.app.state.compiler is original_compiler


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


def test_new_project_can_use_selected_root_or_create_directory(tmp_path: Path) -> None:
    parent = tmp_path / "projects"
    selected = parent / "selected-root"
    selected.mkdir(parents=True)
    (selected / "notes.txt").write_text("keep me", encoding="utf-8")

    with make_client(tmp_path) as client:
        selected_response = client.post(
            "/api/project/new",
            json={"parent": str(selected), "name": None},
        )

        assert selected_response.status_code == 201
        assert selected_response.json() == {
            "workspace": str(selected.resolve()),
            "name": "selected-root",
            "main_file": "main.tex",
        }
        assert "Start writing here." in (selected / "main.tex").read_text(encoding="utf-8")
        assert (selected / "notes.txt").read_text(encoding="utf-8") == "keep me"

        created_response = client.post(
            "/api/project/new",
            json={"parent": str(parent), "name": "created-root"},
        )

        created = parent / "created-root"
        assert created_response.status_code == 201
        assert created_response.json()["workspace"] == str(created.resolve())
        assert created_response.json()["main_file"] == "main.tex"
        assert "Start writing here." in (created / "main.tex").read_text(encoding="utf-8")
        assert client.get("/api/files/main.tex").status_code == 200


def test_new_project_rejects_unsafe_or_existing_roots(tmp_path: Path) -> None:
    parent = tmp_path / "projects"
    parent.mkdir()
    existing = parent / "existing"
    existing.mkdir()
    (existing / "main.tex").write_text("existing manuscript", encoding="utf-8")

    with make_client(tmp_path) as client:
        original = client.get("/api/project").json()
        attempts = [
            ({"parent": "relative", "name": None}, 400),
            ({"parent": str(parent), "name": ""}, 400),
            ({"parent": str(parent), "name": ".hidden"}, 400),
            ({"parent": str(parent), "name": "nested/project"}, 400),
            ({"parent": str(parent), "name": "existing"}, 409),
            ({"parent": str(existing), "name": None}, 409),
        ]
        for payload, expected_status in attempts:
            response = client.post("/api/project/new", json=payload)
            assert response.status_code == expected_status
            assert client.get("/api/project").json() == original

    assert (existing / "main.tex").read_text(encoding="utf-8") == "existing manuscript"
    assert not (parent / ".hidden").exists()
    assert not (parent / "nested").exists()


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


def test_new_project_rebinds_codex_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "main.tex").write_text("first", encoding="utf-8")
    projects = tmp_path / "projects"
    projects.mkdir()
    settings = Settings(CLOVERLEAF_WORKSPACE=workspace, AI_PROVIDER="codex")

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/project/new",
            json={"parent": str(projects), "name": "fresh"},
        )

        created = (projects / "fresh").resolve()
        assert response.status_code == 201
        assert response.json()["workspace"] == str(created)
        assert client.app.state.workspace.root == created
        assert client.app.state.compiler.workspace == created
        assert isinstance(client.app.state.assistant, CodexProvider)
        assert client.app.state.assistant.workspace == str(created)


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


def test_pdf_source_lookup_is_project_scoped_and_rejects_stale_previews(tmp_path: Path) -> None:
    from cloverleaf.models import CompileStatus

    with make_client(tmp_path) as client:
        compiler = client.app.state.compiler
        compiler.status = CompileStatus(state="success", revision=7)
        source = tmp_path / "workspace" / "main.tex"
        calls = []

        async def lookup(page: int, x: float, y: float):
            calls.append((page, x, y))
            return str(source), 12, 0

        compiler.source_location = lookup
        body = {"workspace": str(source.parent), "main_file": "main.tex", "revision": 7, "page": 2, "x": 100, "y": 200}
        response = client.post("/api/pdf/source", json=body)
        assert response.status_code == 200
        assert response.json() == {"path": "main.tex", "line": 12, "column": 0}
        assert calls == [(2, 100, 200)]
        for change in ({"revision": 6}, {"workspace": "/other"}, {"main_file": "other.tex"}):
            assert client.post("/api/pdf/source", json={**body, **change}).status_code == 409
        assert len(calls) == 1
        compiler.status.state = "compiling"
        assert client.post("/api/pdf/source", json=body).status_code == 409
        compiler.status.state = "success"
        source = tmp_path / "outside.tex"
        source.write_text("outside", encoding="utf-8")
        assert client.post("/api/pdf/source", json=body).status_code == 400
        source = tmp_path / "workspace" / ".git" / "config"
        assert client.post("/api/pdf/source", json=body).status_code == 400
        for change in ({"page": 0}, {"x": -1}, {"y": -1}):
            assert client.post("/api/pdf/source", json={**body, **change}).status_code == 422


def test_pdf_source_lookup_reports_missing_mapping(tmp_path: Path) -> None:
    from cloverleaf.models import CompileStatus

    with make_client(tmp_path) as client:
        compiler = client.app.state.compiler
        compiler.status = CompileStatus(state="success", revision=1)
        body = {"workspace": str(tmp_path / "workspace"), "main_file": "main.tex", "revision": 1, "page": 1, "x": 100, "y": 200}
        response = client.post("/api/pdf/source", json=body)
        assert response.status_code == 503
        assert "SyncTeX" in response.json()["detail"] or "recompile" in response.json()["detail"]


def test_pdf_source_lookup_timeout_returns_503(tmp_path: Path) -> None:
    from cloverleaf.models import CompileStatus

    with make_client(tmp_path, raise_server_exceptions=False) as client:
        compiler = client.app.state.compiler
        compiler.status = CompileStatus(state="success", revision=1)

        async def lookup(page: int, x: float, y: float):
            raise asyncio.TimeoutError

        compiler.source_location = lookup
        response = client.post("/api/pdf/source", json={
            "workspace": str(tmp_path / "workspace"), "main_file": "main.tex",
            "revision": 1, "page": 1, "x": 100, "y": 200,
        })

        assert response.status_code == 503
        assert response.json() == {"detail": "Source lookup timed out"}


def test_source_lookup_discards_a_result_when_compilation_starts(tmp_path: Path) -> None:
    from cloverleaf.models import CompileStatus

    with make_client(tmp_path) as client:
        compiler = client.app.state.compiler
        compiler.status = CompileStatus(state="success", revision=7)

        async def lookup(page: int, x: float, y: float):
            compiler.status.state = "compiling"
            return "main.tex", 1, 0

        compiler.source_location = lookup
        response = client.post("/api/pdf/source", json={
            "workspace": str(tmp_path / "workspace"), "main_file": "main.tex",
            "revision": 7, "page": 1, "x": 100, "y": 200,
        })
        assert response.status_code == 409
