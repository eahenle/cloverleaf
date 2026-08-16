# Cloverleaf architecture

Cloverleaf is a local-first two-process application. A Vite/React frontend provides the writing environment; a FastAPI backend owns all filesystem, compilation, and AI-provider access. Manuscript files live under one configured workspace root and are never mixed with application source.

## Components

- **Frontend (`frontend/`)**: React, TypeScript, CodeMirror 6, `react-resizable-panels`, and PDF.js. It uses JSON APIs, debounces saves and compilation requests independently, and polls short-lived compile status. PDF pages render to canvases and retain their approximate scroll position across successful recompiles.
- **Backend (`backend/cloverleaf/`)**: FastAPI routes delegate to small services. `Workspace` resolves and validates every user path beneath the configured workspace. `Compiler` coalesces and serializes `latexmk` jobs, parses common LaTeX diagnostics, and exposes the latest successful PDF. `AssistantProvider` isolates Codex behind a read-only SDK adapter; an OpenAI-compatible adapter remains available as a fallback.
- **Workspace (`workspace/`)**: the default manuscript project. `main.tex` is the configurable compilation root.

The active workspace can be replaced at runtime through the project-load endpoint. A directory-browsing endpoint supplies the in-app folder picker with readable, non-hidden folders and direct `.tex` candidates; it does not return file contents. A folder without a direct `.tex` candidate receives a minimal `main.tex` as part of the confirmed load. A switch otherwise accepts only an existing absolute directory and an existing relative `.tex` compilation root. The backend finishes the current build, constructs a new `Workspace` and `Compiler`, rebinds Codex to the new working directory, and then exposes the new project atomically. Runtime switches do not rewrite `.env`.

## API and trust boundary

The browser never receives provider secrets and never accesses the host filesystem directly. File routes reject absolute paths, traversal (including encoded and backslash forms), symlink escapes, dotfiles, and internal LaTeX build artifacts. Compilation invokes a fixed executable with a fixed argument list from the workspace directory, without a shell or shell escape.

Codex runs server-side with the SDK's read-only sandbox preset. It can read project context supplied in a request and return complete-file proposals, but only Cloverleaf's explicit, confirmed apply flow can write those proposals.

The app binds to localhost by default. This MVP assumes a trusted manuscript and does not claim to sandbox TeX itself.

## Extension seams

`AssistantProvider.chat(messages, context)` can be replaced without changing API consumers. Future project tools can be implemented behind a tool executor while preserving the route and response models. A future project registry can construct one `Workspace` and `Compiler` per selected root without changing their invariants.
