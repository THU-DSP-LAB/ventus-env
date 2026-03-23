# Ventus Perf CLI UX Design

**Goal:** Improve `tools/ventus-perf.py` from a user perspective without changing the underlying perf collection or report semantics.

## Scope

- Keep `tools/ventus-perf.py` as the single user-facing entrypoint.
- Keep the current wrapper/report behavior and supported perf backend boundary unchanged.
- Align the default `VENTUS_BACKEND` value with the driver default: `spike`.
- Improve CLI self-discovery and common failure messages.

## User-Facing Changes

### Default backend handling

- If `VENTUS_BACKEND` is unset, runtime bootstrapping should set it to `spike`.
- Because `ventus_perf` baseline/profiler support remains limited to the phase1 perf backends, an unset backend should now fail explicitly as `spike`, not as an empty string.

### Help output

- Top-level help should explain what the tool does.
- `run --help` should explain:
  - warmup/repeat behavior
  - profiler pass options
  - that the driver default backend is `spike`
  - which backends are currently supported for perf runs
- `report --help` should explain accepted input paths and generated outputs.

### Error presentation

- Expected user/configuration failures should not print Python tracebacks.
- The CLI should render a single-line `error:` message for:
  - unsupported backend selection
  - missing input manifests
  - invalid argument combinations already validated at the CLI layer

### Success presentation

- Successful `run` output should tell the user where the experiment and reports were written.
- Successful `report` output should tell the user where regenerated reports were written.

## Non-Goals

- Expanding perf backend support beyond the current phase1 boundary.
- Changing report schema or attribution logic.
- Adding fallback execution paths or silently skipping failures.
