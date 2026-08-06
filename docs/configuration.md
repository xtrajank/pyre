# Configuration

Every setting pyre reads, in one place. Nothing else is configurable — if you're
looking for a knob that isn't here, it doesn't exist.

There are exactly two kinds of setting:

| | Lives in | Varies by |
|---|---|---|
| **Per source** | [`config/sources.yaml`](../config/sources.example.yaml) | each hub: which field routes, which is the timestamp, whether records are wrapped |
| **Per instance** | App settings | where detections come from, where signals go, where alerts go, where dedup state lives |

Per-source config is in the repo because a list of twenty hubs does not fit in an
app setting. Per-instance config is App settings because it is the *only* thing
that differs between one deployment of pyre and another.

> **App settings are the source of truth.** Function App → **Settings →
> Environment variables → App settings**.
> [`local.settings.json.example`](../local.settings.json.example) mirrors them for
> a local run — it configures nothing in Azure.

---

## The rule this surface follows

**Every "which implementation" decision is a named value, never a presence
check.**

```
DETECTIONS_SOURCE = blob | local
SIGNAL_DESTINATION = blob | http | none
ALERT_DESTINATION  = blob | http | none
STATE_BACKEND      = memory | redis
```

Setting a URL does not switch a mode. Two settings can never disagree about
which one wins. And a selector pointed at something that isn't configured is a
**named entry in `/health` → `problems`**, not a silent drop.

That is what makes changing where this instance writes a settings change rather
than a code change. See
[configuring-destinations.md](configuring-destinations.md).

---

## Identity

Everything pyre touches in Azure — the detection bundle, the output blobs, Redis
— authenticates as the app's managed identity. There are no keys and no
connection strings anywhere in this surface.

| Setting | Covers | Required when |
|---|---|---|
| `AZURE_CLIENT_ID` | **this app's own** Azure calls: detections bundle, output blobs, Redis | the app's identity is **user-assigned** |
| `EVENTHUB_<NAMESPACE>__clientId` | the **host's** Event Hub trigger connection | same |
| `AzureWebJobsStorage__clientId` | the **host's** own storage connection, when that connection is identity-based | same |

**These are three separate settings for three separate consumers, and setting
only the first is the failure where `/health` returns 200 while every trigger
stays silent.** The host resolves the identity per connection; `AZURE_CLIENT_ID`
does not reach it.

With a **system-assigned** identity (the usual case) leave all three empty —
there is a single identity to fall back on. Locally, leave them empty too: your
`az login` / VS Code Azure sign-in is the identity, and it needs the same roles
the deployed identity has.

`AZURE_CLIENT_ID` is the managed identity's **Client ID** (Function App →
**Settings → Identity → User assigned →** the identity **→ Overview**). It is not
a secret.

> `DefaultAzureCredential failed to retrieve a token` reads identically whether
> the identity is off, or is on and pinned to a client id that isn't attached to
> this app. `/health` → `identity` reports both facts directly, including every
> `__clientId` the host will use. The error also lists credential types that can
> never apply here (`EnvironmentCredential`, `WorkloadIdentityCredential`) — read
> the `ManagedIdentityCredential` line, the rest is noise.

---

## Event Hub connections

One pair per `namespace:` in `sources.yaml`. **The name is derived** —
`EVENTHUB_` + the namespace label, uppercased, non-alphanumerics to underscores —
never typed by a source, so it cannot drift from the YAML. `/health` echoes the
exact name each trigger wants under `sources[].connection`.

```
EVENTHUB_<NAMESPACE>__fullyQualifiedNamespace = <namespace>.servicebus.windows.net
EVENTHUB_<NAMESPACE>__credential              = managedidentity
EVENTHUB_<NAMESPACE>__clientId                = <client id>     # user-assigned only
```

The identity needs **Azure Event Hubs Data Receiver** on the namespace. Full
runbook: [adding-a-log-source.md](adding-a-log-source.md).

> **Locally, set only `__fullyQualifiedNamespace`.** `__credential=managedidentity`
> pins the host to a managed identity and your laptop has none, so the trigger
> fails at start-up with a credential error. Omitted, the host falls back to your
> developer sign-in. In the portal, set both.

---

## Detections

Where the detection bundle comes from. See
[writing-detections.md](writing-detections.md).

| Setting | Values | Default | |
|---|---|---|---|
| `DETECTIONS_SOURCE` | `blob` \| `local` | `blob` | `blob` pulls the published bundle from Blob storage. `local` reads a directory on disk — tests and `tools/run_local.py`. |
| `DETECTIONS_BLOB_ACCOUNT_URL` | | | `https://<account>.blob.core.windows.net` — no container, no trailing slash. Required when `blob`. |
| `DETECTIONS_CONTAINER` | | `detections` | Holds `current.json` at its root plus `bundles/`. |
| `DETECTIONS_POINTER` | | `current.json` | The blob naming the live bundle version. |
| `DETECTIONS_LOCAL_DIR` | | `./.bundle` | Used only when `local`. |
| `DETECTIONS_REFRESH_SECONDS` | int | `60` | How fast a published detection goes live. A warm worker makes one cheap pointer read per interval, not one per event. |

Needs **Storage Blob Data Contributor** on that account (Reader is enough to read
the bundle, but the same account usually holds output too).

---

## Destinations

Signals and alerts are independent streams with independent settings.
**[configuring-destinations.md](configuring-destinations.md) is the guide**; this
is the reference.

| Setting | Values | Default |
|---|---|---|
| `SIGNAL_DESTINATION` | `blob` \| `http` \| `none` | `none` |
| `SIGNAL_BLOB_ACCOUNT_URL` | | |
| `SIGNAL_BLOB_CONTAINER` | | `pyre-output` |
| `SIGNAL_HTTP_URL` | | |
| `SIGNAL_HTTP_AUTH_HEADER` | | | 
| `SIGNAL_HTTP_BATCH` | `true` \| `false` | `true` |
| `ALERT_DESTINATION` | `blob` \| `http` \| `none` | `none` |
| `ALERT_BLOB_ACCOUNT_URL` | | |
| `ALERT_BLOB_CONTAINER` | | `pyre-output` |
| `ALERT_HTTP_URL` | | |
| `ALERT_HTTP_AUTH_HEADER` | | |
| `ALERT_HTTP_BATCH` | `true` \| `false` | `false` |
| `HTTP_TIMEOUT_SECONDS` | int | `10` |
| `BLOB_ROLLOVER_MINUTES` | int, must divide 60 | `15` |

`*_HTTP_AUTH_HEADER` is sent whole as the `Authorization` header (`Bearer abc`,
`SharedKey xyz`), so any scheme works without a setting per scheme. **Set it to a
Key Vault reference** — it is the one value in this surface that is a secret.

> An append blob accepts at most 50,000 append operations, ever — past that,
> every further write to it fails. `BlobSink` writes ONE blob per stream per
> `BLOB_ROLLOVER_MINUTES`-wide bucket rather than per day, and every worker
> across every partition writes to the same bucket's blob, so the busier the
> instance the sooner an all-day blob would exhaust that budget. Size the
> bucket so `(worst-case append calls/sec) × (bucket width in seconds)` stays
> comfortably under 50,000 — the default of 15 minutes assumes on the order of
> tens of batches/sec; a much higher sustained rate should use a narrower
> bucket.

---

## State

Dedup windows, thresholds, `unique()` counts, the storm limiter and the
redelivery guard.

| Setting | Values | Default | |
|---|---|---|---|
| `STATE_BACKEND` | `memory` \| `redis` | `memory` | `redis` shares state across workers, which is what makes thresholds and dedup correct under scale-out. `memory` is per worker and resets on a restart. |
| `REDIS_HOST` | | | Azure Cache for Redis hostname. Required when `redis`. |
| `REDIS_PORT` | int | `6380` | TLS. |
| `ALERT_STORM_LIMIT_PER_HOUR` | int | `1000` | Max alerts per detection per hour. Past it, alerts are dropped and signals retained. |

Redis authenticates with **Entra**, not a password — the identity needs the
**Redis Cache Contributor** data-access policy. There is no key to rotate.

> `memory` behaves identically to `redis` **on one worker instance**. Across
> scale-out it does not: two workers count independently and both can alert.
> Setting `STATE_BACKEND=redis` is the entire fix.

---

## Everything else

| Setting | Values | Default | |
|---|---|---|---|
| `INSTANCE_LABEL` | free text | `""` | Echoed by `/health` so a response can be attributed at a glance. Purely cosmetic — no behaviour reads it. |
| `LOG_LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR` | `INFO` | Applies to the `pyre.*` loggers only, so it never turns the Azure SDKs' logging on or off by accident. `INFO` is the one batch line per invocation — see [operations.md](operations.md). |
| `SOURCES_PATH` | path | `<app root>/config/sources.yaml` | Override where the source list is read from. |

---

## Host settings pyre does not read

Set by you, consumed by the Azure Functions runtime.

| Setting | |
|---|---|
| `FUNCTIONS_WORKER_RUNTIME` | `python` |
| `AzureWebJobsStorage` | Not just scratch space: **the Event Hub trigger keeps its checkpoints and partition leases there.** A host with no reachable storage registers the trigger and then never listens. |
| `AzureWebJobsStorage__clientId` | See [Identity](#identity). |
| `AzureFunctionsJobHost__extensions__eventHubs__maxEventBatchSize` | Overrides [`host.json`](../host.json) without a redeploy. Same pattern works for `batchCheckpointFrequency`, `prefetchCount`, `targetUnprocessedEventThreshold`. |
| `SCM_DO_BUILD_DURING_DEPLOYMENT`, `ENABLE_ORYX_BUILD` | Both `true` — build dependencies on the worker from `requirements.txt`. |
| `PYTHON_THREADPOOL_THREAD_COUNT` | Unset by default. On **Flex Consumption** that already defaults to `1000`. On Consumption/Premium/Dedicated with Python 3.9+ (this app runs 3.11) it resolves to `min(32, cpu_count + 4)` instead — still Microsoft's own recommended starting point for I/O-bound work, which is what the Redis round-trips and blob appends in the batch loop are. Usually leave unset either way. |
| `FUNCTIONS_WORKER_PROCESS_COUNT` | Default 1, max 10 — **not available on Flex Consumption at all** (that plan always runs one worker process per instance; scale via more instances instead). On Premium/Dedicated it spawns separate Python **processes** per instance, the only way to get real parallelism on the CPU-bound rule-evaluation loop since threads in one process still share a GIL. Match it to actual cores on the plan there; higher adds context-switch overhead instead of throughput. **Do not raise this above 1 while `STATE_BACKEND=memory`** — each process gets its own in-memory state store, so the same "two workers count independently" problem in [State](#state) happens on a single instance, before scale-out even enters the picture. |

---

## Checking your work

`/health` reports every problem this surface can have, by name:

```json
{
  "status": "ok",
  "problems": [],
  "destinations": { "signal": "blob https://acct.blob.core.windows.net/pyre-output",
                    "alert":  "http https://siem.example/alerts (one record per request)" },
  "detections_source": "blob",
  "state": "redis",
  "identity": { "endpoint": true, "azure_client_id": null,
                "host_connection_client_ids": null }
}
```

`status: ok` and `problems: []` mean the settings are internally consistent. They
do **not** mean the Event Hub listeners attached — that lives in the Functions
host, not in this worker. See
[troubleshooting § Is the trigger actually listening?](troubleshooting.md#is-the-trigger-actually-listening).
