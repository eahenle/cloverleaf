---
name: dev-and-run
description: Develop, validate, live-test, and launch Cloverleaf. Use when changing, fixing, stabilizing, testing, or running this repository and the work should finish with backend, frontend, browser, production-build, and LaTeX checks followed by a healthy headless development server.
---

# Develop and run Cloverleaf

Treat a healthy application in a real browser as the completion condition, not merely passing unit tests.

## Workflow

1. Read `README.md` and `docs/architecture.md`, then inspect `git status` before editing. Preserve unrelated and user-owned changes.
2. Implement the requested change within the existing architecture. Add the smallest practical regression coverage for each substantive defect.
3. Run focused tests while iterating. For UI changes, inspect the result at a desktop viewport and check the browser console and failed network requests.
4. Run `./dev-and-run --check-only` from the repository root. It is the canonical deterministic validation gate and must finish successfully.
5. Run `./cloverleaf` to start the backend and frontend headlessly. Open `http://127.0.0.1:5173`, exercise the affected workflow in a real browser, and confirm `http://127.0.0.1:8000/api/health` succeeds.
6. If the change affects launching, runtime logs, or shutdown, use the Codex panel's Terminal tab and its confirmed shutdown action against the managed server. Restart with `./cloverleaf` afterward unless the user asked for the server to remain stopped.
7. Inspect the final diff and working tree. Report the live workflow, validation results, visual evidence, and any limitation that genuinely remains.

## Entry point contract

- `./dev-and-run` runs the full validation gate and then starts Cloverleaf headlessly.
- `./dev-and-run --check-only` runs the same validation gate without starting the application.
- Do not skip a failed stage or silently replace the root entry point with an ad hoc subset of checks.
- Do not commit or push unless the user requested repository synchronization.
