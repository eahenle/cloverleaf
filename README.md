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
./dev-and-run
```

Open <http://127.0.0.1:5173>. The FastAPI server binds to `127.0.0.1:8000`; Vite binds to `127.0.0.1:5173` and proxies `/api`. The root `./dev-and-run` entry point runs the complete local validation gate and then launches the application headlessly. Use `./cloverleaf` when the checks have already passed and only a launch is needed; it returns after both processes are healthy. Pass a Make target to that wrapper to run another workflow, such as `./cloverleaf test`.

The Codex panel's **Terminal** tab streams the launcher's bounded, tagged backend/frontend stdout and stderr. Its **Shut down server** button asks for confirmation and then cleanly stops both process groups. Runtime state and the rotating 1 MB log live under the ignored `.cloverleaf-runtime/` directory. `make dev` runs the same supervisor in the foreground when terminal-owned Ctrl-C shutdown is preferable.

The default manuscript is `workspace/main.tex`. Relative workspace paths are resolved from the repository root. Change the workspace and compilation root in `.env`:

```dotenv
CLOVERLEAF_WORKSPACE=/absolute/path/to/a/manuscript
CLOVERLEAF_MAIN_FILE=paper.tex
```

`CLOVERLEAF_MAIN_FILE` must be a relative `.tex` path inside that workspace.

While Cloverleaf is running, use **Load project** (the open-folder button in the Project panel) to switch to another local manuscript. The modal folder picker navigates the server's local filesystem with Up, Home, and Computer shortcuts and offers the selected folder's `.tex` files as compilation roots—no path entry is required. If the folder has no root-level `.tex` file, Cloverleaf creates a minimal `main.tex` automatically. The backend validates the selection before switching, waits for any active build to finish, clears project-specific editor and assistant state, and compiles the loaded manuscript. The selection is saved in the ignored `.cloverleaf-project.json` state file and restored after backend restarts or development reloads. Set `CLOVERLEAF_PROJECT_STATE` to move that state file, or remove it to return to the `.env` default.

Project folders start collapsed so large research repositories remain fast and scannable. Cloverleaf watches the open file over one WebSocket connection: clean external edits reload automatically, while a conflicting external edit pauses autosave and preserves the unsaved editor text for review.

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

This checkout can pin the assistant to the personal multi-cli profile by setting `CLOVERLEAF_CODEX_BIN=scripts/codex-personal` in `.env`. The launcher runs `multi-cli codex/personal` while keeping app-server's JSON-RPC output clean. A future authentication settings flow should expose the active account, profile selection, logout, and device-code login in Cloverleaf itself; for now, authentication remains an explicit server-side setup step.

Assistant turns run server-side with the Codex read-only sandbox. A dedicated SDK-level developer preamble makes the standing objective explicit: advance the project's main LaTeX document toward a polished, compiling manuscript, and respond to change requests with concrete file edits rather than suggested wording in chat. Cloverleaf supplies the compilation root, build state and log tail, active file, selected text, and compiler diagnostics, but it does not serialize the project tree into the request. Codex starts in the selected workspace and inspects other files on demand with its read-only tools. The browser never receives provider credentials.

Codex responses use the SDK's structured-output schema with a separate `proposed_edits` channel. To keep responses fast and bounded, Codex returns compact exact-text replacements rather than retransmitting whole existing files. FastAPI validates each replacement against the active workspace, expands it into complete review content, and stamps it with the source version Codex inspected. The UI presents each change as a review card with a compact before/after preview; applying requires confirmation and fails safely if any file changed after Codex prepared the edit. Multi-file responses can be confirmed and applied together as one reviewed set, followed by one compilation. Codex therefore acts on edit requests without silently mutating the live workspace. Explicitly attaching additional files is a future enhancement.

While a turn is running, the assistant panel streams non-sensitive SDK lifecycle phases such as connecting, analyzing, inspecting project files, using tools, retrying, and drafting. It also shows elapsed time, runtime-update count, and a three-second transport heartbeat, so a quiet model turn remains distinguishable from a stalled browser/backend connection. Detailed commands, tool arguments, and reasoning text are intentionally not sent to the browser.

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
./dev-and-run         # run every local check, then launch headlessly
./dev-and-run --check-only # run every local check without launching
./cloverleaf          # start backend + frontend headlessly, then return
make start            # same headless launch without the wrapper
make dev              # foreground supervisor; Ctrl-C stops both processes
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

The browser suite uses the real local backend, `latexmk`, PDF.js, and Chromium at 1440×900. It starts isolated, non-reused test servers on ports 8010 and 5183 so it cannot change or stop an interactive development session. Those direct test servers deliberately report the Terminal tab as unmanaged and disable its shutdown button. The suite serializes tests because cases intentionally edit the same local manuscript, restores fixture content after destructive cases, and captures ignored visual-validation screenshots under `screenshots/`.

## Repository layout

```text
backend/cloverleaf/  FastAPI app, safe workspace operations, compiler, providers
backend/tests/       API, path, file, compiler, and provider tests
frontend/src/        React workbench and primary panels
frontend/tests/      Playwright authoring, compiler, error-state, and visual tests
workspace/           Example three-page LaTeX manuscript
docs/architecture.md Trust boundaries and component design
.codex/skills/       Repository-local Codex development workflows
```

## Security and current scope

Every file API path is resolved beneath the configured workspace. Absolute paths, traversal (including encoded and backslash forms), symlinks, dotfiles, and LaTeX build artifacts are rejected. `latexmk` is invoked without a shell and without enabling shell escape. Generated artifacts do not appear in the project tree.

Cloverleaf binds only to localhost by default. TeX itself is not sandboxed, so compile only manuscripts you trust. This MVP has no authentication, collaboration, database, Git synchronization, or production static-file serving.

## Production frontend build

`make build` writes the optimized frontend to `frontend/dist`. Development intentionally keeps the frontend and backend as separate processes; serving the built bundle from FastAPI is left for a future deployment step.
