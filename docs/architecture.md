# PYRE architecture (streaming)

One page on how it fits together and why each choice is the cheap, scalable one at your volume (Palo Traffic + Cloudflare HTTPRequest, millions/hour each; 100+ schemas; 400+ detections).

## Flow

```
 Cribl (normalize + p_ fields + route)
      |  per source, batched
      v
 Event Hubs  namespace: <prefix>-ehns   hub(s): logs-in (N partitions)
      |  BATCH trigger (100s of events per invocation)
      v
 Function App (Flex Consumption)  ==  the Detection Processor
   for each event in batch:
     1. resolve log_type
     2. enrich (p_enrichment from Redis LUT)      -- optional module
     3. registry.for_log_type(log_type) -> only that source's detections
     4. run rule(); on True -> title/dedup/severity/alert_context/...
     5. write SIGNAL (every match)                -- batched
     6. Redis: INCR dedup key (+TTL) ; threshold ; storm-limit
     7. new alert -> write ALERT + dispatch to destination
      |                         |                       |
      v                         v                       v
  Signals dataset          Redis (state)          Destination adapter
  back to Cribl lake                              (Torq / mock / webhook)
```

## Why these components (cost + scale)

**Event Hubs, not Storage Queue or Service Bus.** At millions/hour, per-message billing and one-at-a-time delivery make queues expensive and slow. Event Hubs is a partitioned log built for this throughput, is the cheapest per-event option, and its Functions trigger delivers **batches**, which is the single biggest cost lever: your execution count becomes `total_events / batch_size`. Cribl has a native Event Hubs destination (Kafka-compatible), so no glue code.

**Batch trigger on Flex Consumption.** Set `maxEventBatchSize` high (start 256-512) in `engine/host.json`. One invocation evaluates the whole batch, amortizing cold start and per-execution cost across hundreds of events. Flex scales to zero when a source goes quiet, and out to ~1000 instances under load. Partitions cap parallelism: give a noisy hub enough partitions (16-32) so enough instances can run concurrently.

**The Lambda-vs-Fargate parallel.** Panther uses Lambda for bursty traffic and Fargate for sustained high volume. Your equivalent: Flex Consumption (serverless, scale-to-zero) for most sources, and **Azure Container Apps** running the same engine image for a source that is high-volume 24/7, where always-on containers beat per-execution billing. The engine package is written so the same code runs under either host; only the entrypoint differs. Start everything on Flex; move a source to Container Apps only if sustained-volume math says so.

**Redis for state.** Dedup, event thresholds, `unique()` counts, and the alert-storm limiter are all stateful and Functions are stateless. Redis gives atomic `INCR` + TTL (the dedup window is just the key's TTL) at sub-millisecond latency. Batch/pipeline the Redis calls once per invocation so a 500-event batch is a handful of round-trips, not 500.

**Signals are cheap because matches are rare.** Most events don't match, so signal volume is a small fraction of ingest volume. Write signals in batches back to a Cribl dataset (HTTP source) so raw events, signals, and alerts are all searchable in one lake.

## Modularity seams

- **Add a log source:** add an entry to `config/sources.yaml` (and, if it needs its own hub for isolation, one `eventhub` module instance). No engine code changes.
- **Add an alert destination:** add an entry to `config/destinations.yaml` and, if it's a new *kind*, one adapter in `engine/pyre_engine/dispatch.py`. Existing destinations untouched.
- **Add/disable a detection:** push a `.py`+`.yml` pair to the external DaC repo (see *Detection freshness* below), or flip `Enabled` via the CLI. No redeploy of infra, no engine change.
- **Scheduled queries (future module):** a separate deployable (timer-triggered Durable orchestrator that runs a Cribl search and fans result rows into the *same* processor entrypoint). It reuses `engine/pyre_engine` wholesale; it does not fork it.

## Detection freshness

Detections are **not** in this repo. They live in an external DaC repo (panther-analysis or a fork); this repo holds only the pointer, [config/detections.yaml](../config/detections.yaml). The requirement is: *a push to the DaC repo must reflect in the running engine fast, at millions of logs/hour across many workers, without a redeploy.*

At that volume the detection code (hundreds of `.py` modules) is loaded into an in-memory `Registry` **once per worker** — never per event. So freshness can't come from re-cloning git on the hot path. Instead **publish is split from load**, with a tiny version pointer between:

```
push to DaC repo
   -> DaC push triggers .azure-pipelines/publish-detections.yml (native ADO
      repository trigger - DaC is an Azure Repos Git repo)
   -> `pyre pull` (clone at ref, filter to dac.path) -> `pyre build` -> `pyre publish`:
      upload bundles/<sha>.zip, THEN flip current.json pointer          [seconds]
   -> each warm worker, at most once per refresh_interval_seconds (default 45s),
      reads the pointer; on change it downloads the zip, rebuilds the Registry,
      and atomically swaps.
```

Cost of freshness: **one cheap pointer read per worker per interval — not per event.** "Time from push to live" ≈ CI publish (seconds) + refresh interval ≈ **under a minute, no redeploy**. Enable/disable rides the same tick (an App Config flag), so it's just as fast.

Two identities, least privilege: the **CI publisher** (an Azure Pipelines Workload Identity Federation service connection, `publisher_principal_id`) *writes* the `detections` container; the **processor** (Managed Identity) *reads* it. No PAT ever reaches the function — cloning the DaC repo inside CI needs no PAT either, since it's a same-org Azure Repos Git repo (the pipeline's own OAuth token reads it). Publish uploads the zip before moving the pointer, so a worker never sees a pointer to a bundle that isn't there.

The whole mechanism lives behind one interface, [engine/pyre_engine/dac.py](../engine/pyre_engine/dac.py) `BundleSource` + [registry.py](../engine/pyre_engine/registry.py) `BundleLoader`. Swapping the poll for an **Event Grid "blob written" push** (sub-second) or the bundle store (`bundle.mode` local ↔ blob) is a one-line change in `source_from_config` — the engine doesn't move. A transient pointer/blob error keeps serving the last-good Registry, so a storage blip never stops detection.

## Non-goals for this repo

Normalization / `p_` field generation lives in **Cribl**, not here (the engine assumes events arrive with `p_log_type`, `p_event_time`, and indicator fields). Search UI, dashboards, correlation rules, replay, and the AI triage agent are separate efforts tracked elsewhere.
