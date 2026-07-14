# detections/

**Detections live in an external Detections-as-Code (DaC) repository, not in this
repo.** This system holds only a *pointer* to that repo:
[config/detections.yaml](../config/detections.yaml).

Point `dac.repo` / `dac.ref` / `dac.path` at your DaC repo (panther-analysis or a
private fork) — a folder of paired `.py` (logic) + `.yml` (metadata) detections,
same contract Panther uses (`rule` + optional `title`/`dedup`/`severity`/
`alert_context`/`description`/`reference`/`runbook`/`destinations`).

## How detections reach the engine

```
your push to the DaC repo
   -> CI/`pyre pull` clones at the pinned ref, filters to dac.path -> a bundle
   -> bundle published (Azure Files mount or Blob) with a version pointer
   -> every warm worker hot-reloads within bundle.refresh_interval_seconds
      (default 45s) — no redeploy.
```

The YAML `LogTypes` field is what routes each log to only its detections; the
engine builds a `LogType -> [detections]` index from the bundle at load time
([engine/pyre_engine/registry.py](../engine/pyre_engine/registry.py)).

## Global helpers (shared code detections import)

Real DaCs keep shared functions in **global helpers** — `.py` files paired with an
`AnalysisType: global` YAML — that detections import by bare name throughout the
repo, e.g. `from panther_base_helpers import deep_get`. These are fully supported:

- List their dirs under `dac.global_helpers` in [config/detections.yaml](../config/detections.yaml)
  (default `[global_helpers]`). They usually sit in a sibling dir *outside* `dac.path`.
- `pyre pull` copies them into the bundle; the engine puts their dirs on the Python
  import path before loading detections, so the imports resolve. Helper edits
  hot-reload just like detection edits. Add `data_models` (or any dir your detections
  import from) to the list.

- `pyre pull` — clone the DaC repo into the local bundle (`.bundle/`).
- `pyre validate` / `pyre build` — run against that bundle.
- Enable/disable a detection at runtime with `pyre enable|disable <id>` (an App
  Config flag the engine re-reads on the same refresh tick — no redeploy).

The swap seam (local dir ↔ Blob ↔ future Event-Grid push) is
[engine/pyre_engine/dac.py](../engine/pyre_engine/dac.py). See
[docs/architecture.md](../docs/architecture.md#detection-freshness).
