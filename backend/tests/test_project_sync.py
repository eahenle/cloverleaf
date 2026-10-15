from pathlib import Path
import subprocess
import time

import pytest

from cloverleaf.project_sync import ProjectSync
from cloverleaf.version_control import LocalGitRepository


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def setup_sync(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(remote), str(seed)], check=True, capture_output=True)
    (seed / "README.md").write_text("Projects\n")
    git(seed, "add", ".")
    git(seed, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Initial")
    git(seed, "push", "origin", "main")
    projects = tmp_path / "local"
    projects.mkdir()
    def project(name):
        root = projects / name
        root.mkdir()
        (root / "main.tex").write_text(name)
        LocalGitRepository(root).initialize()
        return root
    sync = ProjectSync(str(remote), tmp_path / "sync", projects)
    return sync, project, seed, remote


def test_all_projects_and_external_edits_sync_without_secrets_or_builds(setup_sync):
    sync, project, seed, remote = setup_sync
    first, second = project("first"), project("second")
    (first / ".env").write_text("SECRET=do-not-publish")
    (first / "main.pdf").write_bytes(b"build")
    (first / "picture.png").write_bytes(b"image")
    sync.register(first)
    sync.register(second)
    sync.sync_once()
    assert sync.status()["last_error"] is None
    assert git(remote, "show", "main:projects/first/main.tex") == "first"
    assert git(remote, "show", "main:projects/second/main.tex") == "second"
    names = git(remote, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert "projects/first/picture.png" in names
    assert not any(name.endswith((".env", ".pdf")) or "/.git/" in name for name in names)
    (second / "main.tex").write_text("edited outside Cloverleaf")
    sync.sync_once()
    assert git(remote, "show", "main:projects/second/main.tex") == "edited outside Cloverleaf"


def test_cloud_changes_return_locally_and_conflicts_preserve_both_sides(setup_sync):
    sync, project, cloud, remote = setup_sync
    root = project("first")
    sync.register(root)
    sync.sync_once()
    git(cloud, "pull", "--ff-only")
    path = cloud / "projects/first/main.tex"
    path.write_text("cloud edit")
    git(cloud, "add", ".")
    git(cloud, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Cloud edit")
    git(cloud, "push")
    sync.sync_once()
    assert (root / "main.tex").read_text() == "cloud edit"
    (root / "main.tex").write_text("local edit")
    path.write_text("competing cloud edit")
    git(cloud, "add", ".")
    git(cloud, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Competing edit")
    git(cloud, "push")
    sync.sync_once()
    assert "conflict" in sync.status()["last_error"].lower()
    assert (root / "main.tex").read_text() == "local edit"
    assert git(remote, "show", "main:projects/first/main.tex") == "competing cloud edit"


def test_registry_survives_restart_and_remote_deletions_are_applied(setup_sync):
    sync, project, cloud, remote = setup_sync
    root = project("first")
    (root / "notes.md").write_text("notes")
    sync.register(root)
    sync.sync_once()
    git(cloud, "pull", "--ff-only")
    git(cloud, "rm", "projects/first/notes.md")
    git(cloud, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Remove notes")
    git(cloud, "push")
    restarted = ProjectSync(str(remote), sync.state_dir, root.parent)
    restarted.sync_once()
    assert not (root / "notes.md").exists()
    assert restarted.status()["last_error"] is None


def test_symlink_cannot_read_or_write_outside_project(setup_sync, tmp_path):
    sync, project, cloud, remote = setup_sync
    root = project("first")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("private")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    sync.register(root)
    sync.sync_once()
    assert "secret.txt" not in git(remote, "ls-tree", "-r", "--name-only", "main")
    git(cloud, "pull", "--ff-only")
    target = cloud / "projects/first/escape"
    target.mkdir()
    (target / "secret.txt").write_text("cloud overwrite")
    git(cloud, "add", ".")
    git(cloud, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Unsafe path")
    git(cloud, "push")
    sync.sync_once()
    assert (outside / "secret.txt").read_text() == "private"
    assert sync.status()["last_error"]


def test_new_local_and_cloud_projects_are_discovered(setup_sync):
    sync, project, cloud, remote = setup_sync
    project("created-later")
    sync.sync_once()
    assert git(remote, "show", "main:projects/created-later/main.tex") == "created-later"
    git(cloud, "pull", "--ff-only")
    added = cloud / "projects/from-cloud"
    added.mkdir()
    (added / "main.tex").write_text("new cloud project")
    git(cloud, "add", ".")
    git(cloud, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Cloud project")
    git(cloud, "push")
    sync.sync_once()
    sync.sync_once()
    assert (sync.discovery_root / "from-cloud/main.tex").read_text() == "new cloud project"
    assert sync.status()["last_error"] is None


def test_network_failure_retries_without_losing_local_edits(setup_sync):
    sync, project, cloud, remote = setup_sync
    root = project("first")
    sync.sync_once()
    moved = remote.with_name("offline.git")
    remote.rename(moved)
    (root / "main.tex").write_text("offline edit")
    sync.sync_once()
    assert sync.status()["last_error"]
    assert (root / "main.tex").read_text() == "offline edit"
    moved.rename(remote)
    sync.sync_once()
    assert sync.status()["last_error"] is None
    assert git(remote, "show", "main:projects/first/main.tex") == "offline edit"


def test_tracked_but_ignored_sources_survive_repeated_sync(setup_sync):
    sync, project, cloud, remote = setup_sync
    root = project("first")
    (root / ".gitignore").write_text("data.txt\nscratch.txt\n")
    (root / "data.txt").write_text("tracked research data")
    (root / "scratch.txt").write_text("ignored scratch")
    git(root, "add", "--force", "data.txt")
    sync.sync_once()
    assert git(remote, "show", "main:projects/first/data.txt") == "tracked research data"
    sync.sync_once()
    assert (root / "data.txt").read_text() == "tracked research data"
    assert "scratch.txt" not in git(remote, "ls-tree", "-r", "--name-only", "main")


def test_backend_syncs_in_background_and_registers_created_projects(setup_sync):
    from fastapi.testclient import TestClient
    from cloverleaf.config import Settings
    from cloverleaf.main import create_app

    sync, project, cloud, remote = setup_sync
    root = project("first")
    settings = Settings(CLOVERLEAF_WORKSPACE=root, AI_PROVIDER="disabled",
                        CLOVERLEAF_PROJECTS_REMOTE=str(remote),
                        CLOVERLEAF_PROJECTS_SYNC_DIR=str(sync.state_dir),
                        CLOVERLEAF_PROJECTS_SYNC_INTERVAL=1)
    with TestClient(create_app(settings)) as client:
        deadline = time.monotonic() + 10
        while not client.get("/api/projects/sync").json()["last_synced"] and time.monotonic() < deadline:
            time.sleep(0.1)
        assert client.get("/api/projects/sync").json()["last_synced"]
        response = client.post("/api/project/new", json={"parent": str(root.parent), "name": "future"})
        assert response.status_code == 201
        assert "future" in client.get("/api/projects/sync").json()["projects"]
        (root / "main.tex").write_text("inactive project changed")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if git(remote, "show", "main:projects/first/main.tex") == "inactive project changed":
                break
            time.sleep(0.1)
        assert git(remote, "show", "main:projects/first/main.tex") == "inactive project changed"


def test_project_name_collision_keeps_active_and_persisted_selection(setup_sync, tmp_path):
    from fastapi.testclient import TestClient
    from cloverleaf.config import Settings
    from cloverleaf.main import create_app

    sync, project, cloud, remote = setup_sync
    root = project("first")
    other = tmp_path / "elsewhere/first"
    other.mkdir(parents=True)
    (other / "main.tex").write_text("different project")
    settings = Settings(CLOVERLEAF_WORKSPACE=root, AI_PROVIDER="disabled",
                        CLOVERLEAF_PROJECTS_REMOTE=str(remote),
                        CLOVERLEAF_PROJECTS_SYNC_DIR=str(sync.state_dir))
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.post("/api/project/load", json={"workspace": str(other), "main_file": "main.tex"})
        assert response.status_code == 409
        assert client.get("/api/project").json()["workspace"] == str(root)
        assert not settings.project_state_path.exists()
