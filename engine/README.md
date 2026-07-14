# engine/ — the detection processor

This is the heart of pyre: the code that takes a batch of logs, runs the detections against them, and decides what becomes an alert. It's the Azure equivalent of Panther's "streaming detection processor."

New to the terms here (log, detection, signal, dedup, Event Hub, Redis)? Read the [glossary](../docs/GLOSSARY.md) first.

## The big picture

```
Event Hub delivers a BATCH of logs
        │
        ▼
 function_app.py   ← the thin Azure entry point (just receives the batch)
        │  hands the batch to…
        ▼
 pyre_engine/processor.py   ← the real loop, run once per batch:
   for each log:
     1. what LogType is it?                     (skip logs with no type)
     2. enrich it        (enrichment.py — optional extra context)
     3. which detections apply to this LogType? (registry.py — fast lookup)
     4. run each detection's rule(log)          → True/False
     5. on True: write a SIGNAL                 (signals.py — always)
     6. count it toward dedup/threshold         (dedup.py — Redis)
     7. if it crosses the threshold and isn't a duplicate:
          build an ALERT and DISPATCH it        (dispatch.py → Torq/mock)
```

The same `processor.py` will later be reused by the (future) scheduled-query module — it just feeds query-result rows in instead of Event Hub logs. That's why the logic lives in the reusable `pyre_engine` package and `function_app.py` stays tiny.

## The files, in the order they matter

| File | Plain-English job |
|---|---|
| `function_app.py` | The Azure Functions entry point. One function, `detect`, triggered by an Event Hub batch. It decodes the batch and calls the processor. Deliberately thin — no detection logic here. |
| `pyre_engine/processor.py` | The batch loop above. The one file to read to understand pyre's behavior. |
| `pyre_engine/registry.py` | Loads the detection **bundle** (the `.py`+`.yml` files pulled from the external repo) and builds the "LogType → detections" index. Puts **global-helper** dirs (Panther `AnalysisType: global`) on the import path first, so detections that `from panther_base_helpers import …` resolve. Also the **hot-reload** logic (`BundleLoader`) that swaps in new detections within ~45s of a publish, without a redeploy. |
| `pyre_engine/dac.py` | Where the bundle *comes from*: a local directory (dev/test), or Azure Blob (prod, via `BlobBundleSource`). Swapping the source is a one-line change here — the rest of the engine doesn't care. |
| `pyre_engine/dedup.py` | The stateful part, backed by **Redis**: dedup counts, thresholds, `unique()` counts (HyperLogLog), and the storm limiter. All operations for a batch are *pipelined* into a few round-trips for cost. |
| `pyre_engine/enrichment.py` | Optional. Attaches extra context (`p_enrichment`) to a log before rules run. A stub in v1 — fill in if your detections need lookups. |
| `pyre_engine/signals.py` | Writes **signals** (every match) and **alert records** (when one fires) back to the Cribl lake via an HTTP sink, batched. |
| `pyre_engine/dispatch.py` | Sends a fired alert to its **destination(s)** — `mock`, `webhook`, or `torq`. Add a new *kind* here; add a new *instance* in `config/destinations.yaml`. |
| `pyre_engine/config.py` | Reads all runtime settings from environment variables / app settings (never secrets in code) plus `config/detections.yaml`. |
| `pyre_engine/models.py` | The small `Signal` and `Alert` data shapes passed around. |
| `host.json` | Azure Functions host settings. `maxEventBatchSize` (256) is the batch↔single-event ceiling and the main cost lever (executions ≈ total logs ÷ batch size). It's a *ceiling, not a wait*, so it doesn't delay alerts. Override it per-instance from Terraform (`max_event_batch_size` var → the `AzureFunctionsJobHost__…__maxEventBatchSize` app setting); set `1` for single-event. See PRODUCTION.md § 15 "Latency & batching". |
| `requirements.txt` | Python dependencies installed into the Function App. |

## Run it on your laptop (no Azure, $0)

You don't need to deploy to understand or exercise this. From the repo root:

```bash
pip install -r engine/requirements.txt fakeredis
python tools/testlab/run_local.py
```

That runs the real `processor.py` against sample logs, using an in-memory fake Redis and a local sink — printing the signals and alerts it produces. See [tools/README.md](../tools/README.md) and [docs/local-dev.md](../docs/local-dev.md).

## Why it stays cheap at millions of logs/hour

- **Batches, not single logs:** one Function execution handles hundreds of logs.
- **Per-LogType routing:** a Palo log only runs Palo detections, never all 400.
- **Pipelined Redis:** a 256-log batch is a handful of Redis round-trips, not 256.
- **Scale to zero:** when logs stop, the Function App drops to zero instances and costs nothing.

Full reasoning: [docs/architecture.md](../docs/architecture.md).
