# Cloverleaf

**Overleaf at home**: a local-first, desktop-oriented LaTeX writing environment with a project tree, CodeMirror editor, live PDF preview, compiler diagnostics, and a project-aware Codex assistant.

## Prerequisites

- Python 3.11+
- Node.js 20+
- `latexmk` and `pdflatex` (usually supplied by TeX Live or MacTeX)
- Codex authentication for the assistant (`codex login`); the rest of Cloverleaf works without it

On Fedora, the TeX packages can be installed with `sudo dnf install latexmk texlive-scheme-basic`. On macOS, install MacTeX. Package names vary by Linux distribution.

## Start development

```bash
cp .env.example .env
make install
make dev
```

Open <http://127.0.0.1:5173>. The FastAPI server listens only on `127.0.0.1:8000`; Vite proxies `/api` during development. `make dev` runs both processes and stops both on Ctrl-C.

The default manuscript is `workspace/main.tex`. Change `CLOVERLEAF_WORKSPACE` and `CLOVERLEAF_MAIN_FILE` in `.env` to point Cloverleaf at another project. Relative workspace paths are resolved from the directory where the backend starts.

## Assistant configuration

Codex is the default provider:

```dotenv
AI_PROVIDER=codex
AI_MODEL=gpt-5.6-sol
```

The backend uses the Codex Python SDK and the machine's existing Codex login. Assistant turns run with a read-only sandbox. Codex can return proposed complete-file replacements, but Cloverleaf shows each proposal for confirmation before writing it.

An OpenAI-compatible fallback remains behind the same provider interface:

```dotenv
AI_PROVIDER=openai-compatible
AI_BASE_URL=https://api.example.com/v1
AI_API_KEY=...
AI_MODEL=...
```

Secrets are read only by FastAPI and are never sent to browser code.

## Commands

```bash
make dev       # backend + frontend with reload
make backend   # FastAPI only
make frontend  # Vite only
make test      # backend tests
make lint      # ESLint + TypeScript typecheck
make build     # production frontend build
make compile   # compile the example manuscript directly
```

FastAPI also exposes interactive API documentation at <http://127.0.0.1:8000/docs>.

## Repository layout

```text
backend/cloverleaf/  FastAPI app, safe workspace operations, compiler, providers
backend/tests/       Path, file operation, diagnostic, and provider parsing tests
frontend/src/       React workbench and four primary panels
workspace/          Example LaTeX manuscript (not application source)
docs/architecture.md
```

## Security and current scope

Every file API path is resolved beneath the configured workspace; absolute paths, traversal, and symlink escapes are rejected. `latexmk` is invoked without a shell and without shell escape. Build artifacts are hidden from the project tree. Cloverleaf binds to localhost by default.

TeX itself is not sandboxed in this MVP, so only compile manuscripts you trust. There is no authentication, collaboration, database, Git synchronization, or multi-project picker yet.

## Production-ish local build

`make build` creates `frontend/dist`. The MVP intentionally keeps frontend and backend development processes separate; serving the built static bundle from FastAPI is a straightforward next deployment step.
