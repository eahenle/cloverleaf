import pytest


@pytest.fixture(autouse=True)
def disable_personal_project_sync(monkeypatch):
    """Tests must never synchronize the developer's real project repository."""
    monkeypatch.setenv("CLOVERLEAF_PROJECTS_REMOTE", "")
