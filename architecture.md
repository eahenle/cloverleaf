# Cloverleaf architecture

Cloverleaf is a local-first two-process application. A Vite/React frontend provides the writing environment; a FastAPI backend owns all filesystem, compilation, and AI-provider access. Manuscript files live under one configured workspace root and are never mixed with application source.

## Components

- **Frontend (`frontend/`)**: React, TypeScript, CodeMirror 6, `react-resizable-panels`, and PDF.js. It uses ordinary JSON APIs and polls the short-lived compile status. Local editor state is saved after a debounce; compilation has a separate longer debounce.
- **Backend (`backend/cloverleaf/`)**: FastAPI routes delegate to small services. `Workspace` resolves and validates every user path beneath the configured workspace. `Compiler` serializes `latexmk` jobs with an async lock, parses common LaTeX diagnostics, and exposes the resulting PDF. `AssistantProvider` isolates Codex behind a read-only SDK adapter; an OpenAI-compatible adapter remains available as a fallback.
- **Workspace (`workspace/`)**: the default manuscript project. `main.tex` is the configurable compilation root.

## API and trust boundary

The browser never receives provider secrets and never accesses the host filesystem directly. File routes reject absolute paths, `..` traversal, symlink escapes, and internal LaTeX build artifacts. Compilation invokes a fixed executable with a fixed argument list, from the workspace directory, with no shell and no shell escape.

The app binds to localhost by default. This MVP assumes a trusted manuscript and does not claim to sandbox TeX itself.

## Extension seams

`AssistantProvider.chat(messages, context)` can be replaced without changing API consumers. Assistant responses may include typed proposed edits that the UI presents for explicit review. Future tools—read/search/patch/create/compile/diagnostics—can be implemented behind a tool executor while preserving the route and response model. A future project registry can construct one `Workspace` and `Compiler` per selected root without changing their invariants.
