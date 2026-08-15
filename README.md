# Cloverleaf

**Overleaf at home**: a local-first, desktop-oriented LaTeX writing environment with a project tree, CodeMirror editor, live PDF preview, compiler diagnostics, and a project-aware Codex assistant.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) and Python 3.10+
- Node.js 20+ and npm
- `latexmk` and `pdflatex`
- Codex CLI authentication for the assistant; the rest of Cloverleaf works without it

Install TeX Live on macOS with `brew install texlive` (or install MacTeX). On Fedora, use `sudo dnf install latexmk texlive-scheme-basic`. Package names vary by distribution.

## Install and start

```bash
cp .env.example .env
make install
./cloverleaf
```

Open <http://127.0.0.1:5173>. The FastAPI server binds to `127.0.0.1:8000`; Vite binds to `127.0.0.1:5173` and proxies `/api`. The root `./cloverleaf` entry point defaults to `make dev`, runs both processes, and stops both on Ctrl-C. Pass a Make target to run another workflow, such as `./cloverleaf test`.

The default manuscript is `workspace/main.tex`. Relative workspace paths are resolved from the repository root. Change the workspace and compilation root in `.env`:

```dotenv
CLOVERLEAF_WORKSPACE=/absolute/path/to/a/manuscript
CLOVERLEAF_MAIN_FILE=paper.tex
```

`CLOVERLEAF_MAIN_FILE` must be a relative `.tex` path inside that workspace.

## Codex assistant

Authenticate the installed Codex CLI before starting Cloverleaf:

```bash
codex login
codex login status
```

Codex is the default provider:

```dotenv
AI_PROVIDER=codex
AI_MODEL=gpt-5.6-sol
CLOVERLEAF_CODEX_BIN=
```

The backend uses the official [Codex Python SDK](https://developers.openai.com/codex/codex-sdk) and the machine's existing Codex login. It uses the authenticated local `codex` executable when available, with the SDK-pinned runtime as fallback. Set `CLOVERLEAF_CODEX_BIN` only when a specific executable is required.

Assistant turns run server-side with the Codex read-only sandbox. The browser sends manuscript context—not credentials—to FastAPI. Codex can return complete-file proposals, but Cloverleaf shows each proposal as a review card and requires confirmation before writing it.

An OpenAI-compatible fallback remains behind the same provider interface:

```dotenv
AI_PROVIDER=openai-compatible
AI_BASE_URL=https://api.example.com/v1
AI_API_KEY=...
AI_MODEL=...
```

Provider secrets are read only by FastAPI and are never included in frontend bundles or assistant request payloads.

## Development commands

```bash
make install          # Python and frontend dependencies
make dev              # backend + frontend with reload
./cloverleaf          # root entry point; defaults to make dev
./cloverleaf test     # delegate to any documented Make target
make backend          # FastAPI only on 127.0.0.1:8000
make frontend         # Vite only on 127.0.0.1:5173
make test             # backend pytest suite
make browser-install  # install Playwright Chromium once
make test-e2e         # deterministic browser suite; live Codex test is skipped
make test-live-codex  # authenticated live Codex browser workflow
make lint             # ESLint
make typecheck        # TypeScript typecheck
make build            # typecheck + production Vite build
make compile          # compile workspace/main.tex directly
make clean            # remove LaTeX build artifacts
```

FastAPI exposes interactive API documentation at <http://127.0.0.1:8000/docs> while the backend is running.

The browser suite uses the real local backend, `latexmk`, PDF.js, and Chromium at 1440×900. It serializes tests because they intentionally edit the same local manuscript, and it restores fixture content after destructive cases. It also captures ignored visual-validation screenshots under `screenshots/`.

## Repository layout

```text
backend/cloverleaf/  FastAPI app, safe workspace operations, compiler, providers
backend/tests/       API, path, file, compiler, and provider tests
frontend/src/        React workbench and primary panels
frontend/tests/      Playwright authoring, compiler, error-state, and visual tests
workspace/           Example three-page LaTeX manuscript
docs/architecture.md Trust boundaries and component design
```

## Security and current scope

Every file API path is resolved beneath the configured workspace. Absolute paths, traversal (including encoded and backslash forms), symlinks, dotfiles, and LaTeX build artifacts are rejected. `latexmk` is invoked without a shell and without enabling shell escape. Generated artifacts do not appear in the project tree.

Cloverleaf binds only to localhost by default. TeX itself is not sandboxed, so compile only manuscripts you trust. This MVP has no authentication, collaboration, database, Git synchronization, multi-project picker, or production static-file serving.

## Production frontend build

`make build` writes the optimized frontend to `frontend/dist`. Development intentionally keeps the frontend and backend as separate processes; serving the built bundle from FastAPI is left for a future deployment step.
