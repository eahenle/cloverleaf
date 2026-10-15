"""Run one project sync using the same configuration as the Cloverleaf backend."""
import argparse
import json

from cloverleaf.config import Settings
from cloverleaf.project_sync import ProjectSync


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", help="override CLOVERLEAF_PROJECTS_REMOTE for this run")
    args = parser.parse_args()
    settings = Settings()
    sync = ProjectSync(args.remote or settings.projects_remote, settings.projects_sync_path,
                       settings.workspace.parent, settings.projects_sync_interval)
    if not sync.remote:
        parser.error("Set CLOVERLEAF_PROJECTS_REMOTE or pass --remote")
    sync.sync_once()
    print(json.dumps(sync.status(), indent=2))
    return 1 if sync.last_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
