# Ventus Perf Rename Design

**Goal:** Rename the user-facing Ventus perf tool entrypoint from `tools/ventus_perf.py` to `tools/ventus-perf.py`, sync user-visible references, and remove unused import shims.

## Scope

- Rename the user-facing script to `tools/ventus-perf.py`.
- Update README, tests, CLI help text, and recent design/plan docs that mention the old script path.
- Keep the internal Python package name `ventus_perf` unchanged.
- Keep the internal implementation directory `tools/ventus_perf/` unchanged unless a clean Python-safe rename is clearly beneficial.
- Remove the repo-root `ventus_perf` shim if it no longer serves a purpose.

## Design

### User-facing path

- `tools/ventus-perf.py` becomes the only supported direct entrypoint.
- All user-visible examples and tests should call `python3 tools/ventus-perf.py ...`.

### Internal package layout

- The implementation remains under `tools/ventus_perf/`.
- Python modules continue to import `ventus_perf.*` so no dynamic import hacks or compatibility wrappers are needed.

### Root shim cleanup

- The repo-root `ventus_perf/__init__.py` currently exists only to redirect imports into `tools/ventus_perf/`.
- If tests and direct script execution still work without it, delete the shim directory rather than carrying an extra compatibility layer with no clear owner.

## Non-Goals

- Renaming the build output directory `build/ventus-perf/`.
- Renaming driver/runtime `VENTUS_PERF*` environment variables.
- Renaming internal Python import names to use hyphens.
