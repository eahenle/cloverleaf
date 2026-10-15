"""Run the browser-test backend against a disposable copy of the example project."""
import os
from pathlib import Path
import shutil
import tempfile

import uvicorn


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="cloverleaf-browser-") as temporary:
        test_root = Path(temporary)
        workspace = test_root / "workspace"
        shutil.copytree(root / "workspace", workspace, ignore=shutil.ignore_patterns(".git", ".cloverleaf"))
        os.environ.update({
            "CLOVERLEAF_WORKSPACE": str(workspace),
            "CLOVERLEAF_MAIN_FILE": "main.tex",
            "CLOVERLEAF_PROJECT_STATE": str(test_root / "selection.json"),
            "CLOVERLEAF_PROJECTS_REMOTE": "",
        })
        uvicorn.run("cloverleaf.main:app", host="127.0.0.1", port=8010)


if __name__ == "__main__":
    main()
