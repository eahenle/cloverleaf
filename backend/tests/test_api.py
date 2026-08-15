from pathlib import Path

from fastapi.testclient import TestClient

from cloverleaf.config import Settings
from cloverleaf.main import create_app


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
        response = client.get("/api/pdf")
        assert response.status_code == 404
        assert response.json()["detail"] == "No successful PDF build is available yet"
