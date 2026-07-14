# config/ — the declarative settings

Everything in here is **plain text you edit, not code you write**. These files are read by *both* the Terraform (to build the right cloud resources) and the engine (at runtime), so "where detections come from," "where logs arrive," and "where alerts go" are configuration, not hard-coded logic. Change a file here and redeploy/republish — you never edit engine code to onboard a log source or add a destination.

New to the terms (DaC, bundle, LogType, destination)? See the [glossary](../docs/GLOSSARY.md).

## The files

| File | What it controls | Who reads it |
|---|---|---|
| `detections.yaml` | **Where the detections live** — the external DaC repo (URL, branch, folder), the Git token variable, and how the bundle is fetched + hot-reloaded. This repo holds *only this pointer*; no detection code. | the `pyre` CLI (`pull`/`publish`) and the engine (which bundle to load) |
| `sources.yaml` | The log **sources**: each one's LogTypes and how big an Event Hub it needs (partitions = parallelism). Add a source = add an entry. | Terraform (sizes Event Hubs) and the engine (reads `p_log_type` per log) |
| `destinations.yaml` | Where alerts **go**: `mock` (test sink), `webhook`, `torq`. Add an *instance* here; add a new *kind* only by editing `engine/pyre_engine/dispatch.py`. Secrets come from Key Vault via `*_env` references, never inline. | the engine (dispatch) |
| `envs/dev.yaml`, `envs/prod.yaml` | Per-environment overrides (cost profile, default alert routes). | tooling |

## `detections.yaml` in depth (the important one)

This is the file that makes detections external. Key fields:

- `dac.repo` / `dac.ref` / `dac.path` — the detections repo, the branch/tag/commit to pin, and the subfolder inside it that holds the detections.
- `dac.global_helpers` — sibling dirs of shared `.py` modules your detections import by name (Panther `AnalysisType: global`, e.g. `from panther_base_helpers import ...`). `pyre pull` bundles them and the engine puts them on the import path so the imports resolve. Default `[global_helpers]`; add `data_models` etc. as needed.
- `dac.token_env` — the name of the environment variable holding a Git token (for a *private* repo). Leave the variable unset for a public repo. This token is used **only** to clone, and **only** by the CLI/CI — the running engine never sees it.
- `bundle.mode` — `local` (read a directory; used on your laptop and by tests) or `blob` (pull a published bundle from Azure; used by the deployed engine).
- `bundle.refresh_interval_seconds` — the upper bound on how long after a `publish` a running worker takes to hot-reload the new detections (default 45s).

To point pyre at your team's detections, edit `dac.repo`/`dac.ref`/`dac.path` here and run `python cli/pyre pull`. Nothing else changes.

## Example: onboard a new log source

1. Add an entry to `sources.yaml` (its LogTypes, a hub, a partition count).
2. `terraform apply` — the Event Hub is sized for it. No engine change.
3. Make sure Cribl stamps `p_log_type` for that source and sends it to the hub.

## Example: send alerts to real Torq instead of the mock

1. In `destinations.yaml`, enable the `torq_dev` destination and set its `url_env`/`token_env`.
2. Store the token in Key Vault; the engine reads it by reference at runtime.
3. Point the environment's default route at `torq_dev`. No engine code change.
