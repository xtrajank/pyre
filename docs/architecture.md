# PYRE architecture (streaming)

One page on how it fits together and why each choice is the cheap, scalable one at your volume (Palo Traffic + Cloudflare HTTPRequest, millions/hour each; 100+ schemas; 400+ detections).

## Flow

```
 Cribl (normalize + p_ fields + route)
      |  per source, batched
      v
 Event Hubs  namespace: <prefix>-ehns   hub(s) from config/sources.yaml (N partitions)
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

## Hubs: dedicated for isolation, one catch-all for everything else

`config/sources.yaml` is the single declaration of the hub set. Terraform creates
one hub per entry and subscribes the processor to **all** of them —
`engine/function_app.py` registers one Event Hubs trigger per hub, because a
trigger binds to exactly one. Exactly one entry sets `default: true`.

**Which hub an event lands in is Cribl's decision, not pyre's.** By the time the
engine sees an event it is already in a hub; the engine reads the log-type field
only to pick detections. So the hub an event arrived on has **no bearing on how
it is handled** — a log type on the catch-all is routed, signalled, deduped and
alerted exactly as it would be on its own hub.

A dedicated hub therefore buys two things and only two: **isolation** (Palo's
firehose can't starve Cloudflare) and **its own parallelism ceiling** (partition
count). The catch-all gets the fewest partitions on purpose — it carries the long
tail. The tradeoff is explicit: a high-volume source that falls through to it is
capped at that partition count and will lag, and that lag is the signal to give
it its own entry.

This is what lets `pyre validate` accept a 1000-detection panther-analysis fork
without demanding an Event Hub per log type: an undeclared log type has a real
home, so it is a summary line rather than an error. Without a `default: true`
hub, validate goes back to erroring — because then it genuinely has nowhere to
land, and a detection for it could never see an event.

The failure this structure exists to prevent is silent: a hub with no consumer,
or a trigger bound to a hub that doesn't exist, accepts logs at full rate and
evaluates none of them with no error anywhere. Both directions are asserted
offline in `infra/tests/plan.tftest.hcl`.

## Modularity seams

- **Add a log source:** add an entry to `config/sources.yaml`. Terraform creates and sizes the hub, and the processor picks up a trigger for it on the next deploy. No engine code changes. (Point Cribl at it, or it stays idle.)
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

Detection **code** is hot-reloaded; the **modules a detection is loaded next to are cached per log type**, so a bundle of 900 detections spanning 90 log types costs a worker only the imports its own traffic can actually match — see `Registry.for_log_type` in [registry.py](../engine/pyre_engine/registry.py). Building the index is YAML-parse-only and executes no detection code.

### What hot-reload does NOT cover: the Python environment

**A DaC push can ship code, never a package.** Third-party dependencies come from [engine/requirements.txt](../engine/requirements.txt) and are installed when the Function App is **deployed** — baked into the deployment package that each worker mounts at cold start. Nothing re-reads that file at runtime, and a bundle reload is a `.py`/`.yml` swap with no pip step. The two lifecycles are decoupled:

| | changed by | goes live via | latency |
|---|---|---|---|
| detection code | a DaC push | bundle reload | ~45s, no redeploy |
| the packages it imports | editing `engine/requirements.txt` | **redeploying the engine** | a deploy |

So a detection that adds `import dateutil` publishes fine and then fails to import on every worker. It is skipped, and the only symptom is a `skipping detection ...` warning — a **silent coverage hole**, which is exactly the failure a detection engine must not have.

The publish pipelines therefore gate on **`pyre deps`** ([deps.py](../engine/pyre_engine/deps.py)) with `engine/requirements.txt` installed: it `ast`-parses every rule and global-helper file (never importing them — that would run DaC code in CI) and resolves each import against the real environment, so import-name-vs-package-name (`dateutil` → `python-dateutil`) is whatever pip actually provides. A bundle whose imports don't resolve **fails the pipeline instead of publishing**. Adding a dependency is deliberately a two-step, human-approved change: update `requirements.txt`, redeploy, *then* the DaC push goes green — pip-installing whatever a DaC push asks for would let the detections repo execute arbitrary code in the engine.

The gate is only as truthful as CI's Python matching the runtime, so `UsePythonVersion`/`setup-python` in both publish pipelines must track `runtime_version` in [modules/function_app](../infra/modules/function_app/main.tf).

Two identities, least privilege: the **CI publisher** (`var.publisher` — either its own Managed Identity if the agent runs on Azure compute, or a Workload Identity Federation trust `module.external_identity` provisions if it doesn't) *writes* the `detections` container; the **processor** (Managed Identity) *reads* it. No PAT ever reaches the function — cloning the DaC repo inside CI needs no PAT either, since it's a same-org Azure Repos Git repo (the pipeline's own OAuth token reads it). Publish uploads the zip before moving the pointer, so a worker never sees a pointer to a bundle that isn't there.

The whole mechanism lives behind one interface, [engine/pyre_engine/dac.py](../engine/pyre_engine/dac.py) `BundleSource` + [registry.py](../engine/pyre_engine/registry.py) `BundleLoader`. Swapping the poll for an **Event Grid "blob written" push** (sub-second) or the bundle store (`bundle.mode` local ↔ blob) is a one-line change in `source_from_config` — the engine doesn't move. A transient pointer/blob error keeps serving the last-good Registry, so a storage blip never stops detection.

## Failure model: fail toward duplicates, never toward loss

Event Hubs delivers **at-least-once**, and neither Redis nor Cribl can enlist in a transaction with it. So exactly-once isn't on the menu — the only real choice is *which way to fail*. Everything below chooses duplicates: a duplicate signal is noise, a lost one is a match that provably happened and that nobody can ever see again.

**Claim, do, release.** [processor.py](../engine/pyre_engine/processor.py) claims every event id up front (one pipelined `SET NX` per batch) so a redelivery can't double-count. The subtlety is that the claim is committed *before* the work: if a later phase fails, the claim must be **released**, or Event Hubs redelivers the batch, every id reads as already-processed, the batch is skipped clean, and the matches are gone with nothing logged. What a redelivery costs is bounded and known: signals may duplicate, thresholds may fire slightly early, and alerts do **not** duplicate because `register_alert`'s `SET NX` marker outlives the retry.

**Failures must be loud.** `requests` does not raise on 5xx — it returns a `Response`. Every sink call therefore checks status explicitly; without that, a Cribl outage is byte-for-byte indistinguishable from success. A failed alert *delivery* is the one case that doesn't fail the batch: the signal is the record of record and is worth keeping even when Torq is down, so the alert's dedup window is re-opened instead and a later match re-fires it.

**A detection is untrusted code.** `rule`/`title`/`dedup`/`severity`/`alert_context`/`destinations` all come from the DaC repo, so each is wrapped: an exception is that detection's problem, tallied per batch, and never the batch's. Unguarded, one bad `title()` would fail the invocation — and because Event Hubs redelivers, it would poison the partition permanently, retrying the same event forever.

**State is bounded on purpose.** Every Redis key has a TTL, and the read-modify-write that arms it is a Lua script rather than two client commands ([dedup.py](../engine/pyre_engine/dedup.py)) — an `INCR` that lands while its `EXPIRE` fails leaves a key that never expires. (It's also portability: `EXPIRE key ttl NX` is Redis 7.0+, while Azure Cache for Redis Basic/Standard/Premium is 6.0 and rejects it. fakeredis accepts it, so no local test would ever catch that.) Key size is bounded too — detection-authored dedup strings are hashed rather than embedded.

**Observability must scale with problems, not with traffic.** At 4-5TB/day a single `log.info` per event is a ~50k records/sec firehose that costs the most exactly when the system is least healthy. Counts are aggregated per batch. The corollary: things that used to be a bare `continue` — malformed JSON, a missing log-type field, a log type no detection covers — are now counted, because at this volume a silently dropped feed is invisible until an alert doesn't fire.

## Non-goals for this repo

Normalization / metadata-field generation lives in **Cribl**, not here — the engine only assumes events arrive with *some* log-type field and *some* event-time field, plus indicator fields; which field names those are is a Terraform setting (`log_type_field`/`event_time_field`, default to Cribl's own `dataset`/`_time`), not something hard-coded to Cribl's or Panther's convention. Search UI, dashboards, correlation rules, replay, and the AI triage agent are separate efforts tracked elsewhere.
