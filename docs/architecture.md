# Cloverleaf architecture

Cloverleaf is a local-first two-process application managed by a small launcher. A Vite/React frontend provides the writing environment; a FastAPI backend owns all filesystem, compilation, runtime-control, and AI-provider access. Manuscript files live under one configured workspace root and are never mixed with application source.

## Components

- **Frontend (`frontend/`)**: React, TypeScript, CodeMirror 6, `react-resizable-panels`, and PDF.js. It uses JSON APIs, debounces saves and compilation requests independently, and polls short-lived compile status. PDF pages render to canvases and retain their approximate scroll position across successful recompiles.
- **Backend (`backend/cloverleaf/`)**: FastAPI routes delegate to small services. `Workspace` resolves and validates every user path beneath the configured workspace. `Compiler` coalesces and serializes `latexmk` jobs, parses common LaTeX diagnostics, and exposes the latest successful PDF. `AssistantProvider` isolates Codex behind a read-only SDK adapter; an OpenAI-compatible adapter remains available as a fallback.
- **Launcher (`scripts/dev.py`)**: supervises backend and frontend process groups with no terminal input, combines their stdout/stderr into a tagged 1 MB rotating log, and owns the shutdown lifecycle. The root entry point detaches this supervisor; `make dev` keeps only the supervisor in the foreground. Development reload shutdown is bounded so persistent file/log WebSockets cannot indefinitely prevent a changed backend from restarting.
- **Workspace (`workspace/`)**: the default manuscript project. `main.tex` is the configurable compilation root.

The active workspace can be replaced at runtime through the project-load endpoint. A directory-browsing endpoint supplies the in-app folder picker with readable, non-hidden folders and direct `.tex` candidates; it does not return file contents. A folder without a direct `.tex` candidate receives a minimal `main.tex` as part of the confirmed load. A switch otherwise accepts only an existing absolute directory and an existing relative `.tex` compilation root. The backend finishes the current build, persists the selection in an ignored local state file, constructs a new `Workspace` and `Compiler`, rebinds Codex to the new working directory, and then exposes the new project atomically. Startup restores a still-valid saved selection, including after development reloads, without rewriting `.env`. Assistant turns also verify Codex's working directory against the authoritative backend workspace before starting.

## API and trust boundary

The browser never receives provider secrets and never accesses the host filesystem directly. File routes reject absolute paths, traversal (including encoded and backslash forms), symlink escapes, dotfiles, and internal LaTeX build artifacts. Compilation invokes a fixed executable with a fixed argument list from the workspace directory, without a shell or shell escape.

When the launcher is present, it passes FastAPI absolute paths for its runtime log and a private shutdown sentinel. FastAPI can stream the bounded log, but it does not expose a terminal or arbitrary process signaling. A confirmed shutdown request writes only that sentinel; the supervisor remains responsible for terminating its own child process groups. Direct backend launches have neither path and therefore expose no logs and reject shutdown.

Codex runs server-side with the SDK's read-only sandbox preset and the active project as its working directory. SDK-level developer instructions establish its standing LaTeX-authoring objective and require change requests to produce concrete edits rather than suggested replacement text in the conversation. Per-turn context supplies the authoritative compilation root plus the active build state, bounded log tail, open file, selection, and structured diagnostics; other files are discovered agentically with read-only runtime tools.

The SDK enforces a structured response with separate message and compact exact-replacement fields. FastAPI validates every returned path through `Workspace`, requires each existing-file match to be unique, expands the replacements in order into complete review content, rejects duplicate or unsafe targets, and attaches the current file version before returning a review card. Only Cloverleaf's explicit, confirmed apply flow can write an edit; its optimistic version checks validate an entire reviewed set before any file is changed. Multiple accepted cards are written together and trigger one compilation. Each assistant POST has a short-lived, request-scoped WebSocket: FastAPI translates selected SDK turn/item/retry events into stable non-sensitive phases and emits a heartbeat during quiet periods while the ordinary JSON request retains normal success and error semantics. Raw reasoning, command text, tool arguments, and credentials remain server-side.

The app binds to localhost by default. This MVP assumes a trusted manuscript and does not claim to sandbox TeX itself.

## Extension seams

`AssistantProvider.chat(messages, context)` can be replaced without changing API consumers. Future project tools can be implemented behind a tool executor while preserving the route and response models. A future project registry can construct one `Workspace` and `Compiler` per selected root without changing their invariants.
