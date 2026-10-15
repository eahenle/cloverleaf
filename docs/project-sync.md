# Shared Cloverleaf projects

The shared repository stores source files under `projects/<project-directory-name>/`.
Local directories and their individual Git histories stay in place. The sync service
uses its own clone, so it never pushes a project's existing origin or changes its
staging area. The existing `job_hunt` history is preserved in the shared repository.

Configure the backend in `.env`:

```dotenv
CLOVERLEAF_PROJECTS_REMOTE=git@github.com:eahenle/cloverleaf_projects.git
CLOVERLEAF_PROJECTS_SYNC_INTERVAL=30
```

The backend syncs immediately at startup and every 30 seconds thereafter. It discovers
directories containing `main.tex` beside the configured workspace, and registers every
project created or loaded through Cloverleaf, including projects elsewhere on disk.
Registration persists across restarts. Unique directory names identify projects;
colliding names are reported instead of combining unrelated projects.

Cloud agents edit `projects/<name>/` in the shared repository. New cloud project
directories are imported beside the configured workspace. Each cycle fetches and
fast-forwards the shared clone, compares local and cloud file contents against the
last successful sync, imports cloud-only changes, commits local-only changes, and
pushes to `main`. External edits and inactive projects are included. New files,
renames, deletions, images, reference PDFs, and hidden project skills are supported.
PDFs with corresponding TeX sources are treated as generated output and excluded.

Local and cloud changes to the same file pause synchronization and preserve both
versions. View the conflicting paths in **Status → GitHub project sync** or
`GET /api/projects/sync`. Reconcile the content so the local and cloud versions
agree, and the next cycle resumes. Network errors retry on the next cycle without
discarding local edits. A diverged shared-clone branch is reported for manual review;
the service never force-pushes or resets project files.

The service excludes Git internals, symlinks, `.env` files, private key files,
credential stores, generated LaTeX artifacts, caches, and `.cloverleaf/` conversation
state. Existing project ignore rules also apply to new local files. It uses ordinary
Git and the host's configured authentication; no credentials enter the browser.

The registry, per-file synchronization baselines, and shared clone live in
`.cloverleaf-runtime/projects-sync/` beside the project-selection state file.
`CLOVERLEAF_PROJECTS_SYNC_DIR` can override that location. Do not delete this state
to resolve a conflict: the baselines distinguish local and cloud changes. The cache
must be clean before a cycle starts. Interrupted cache writes require inspection.

To sync explicitly using the same configuration:

```bash
uv run python scripts/sync_projects.py
```

Automatic syncing runs while the Cloverleaf backend is running. Tests disable the
configured remote and use disposable local bare repositories for sync coverage.
The browser-test backend uses a temporary copy of the example project so a running
production sync cannot publish transient test edits.
