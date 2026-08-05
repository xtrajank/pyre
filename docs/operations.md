# Operations

How to tell what pyre is doing, from the outside.

Two surfaces: **one log line per batch**, and **`/health`**. That is deliberately
all of it — a detection engine that logs per event is unreadable exactly when you
need to read it.

---

## The batch line

Every Event Hub invocation emits exactly one INFO line, whatever the volume:

```
batch platform/applog msgs=3 events=7 new=7 signals=4 alerts=1 12ms
```

| Field | |
|---|---|
| `platform/applog` | `namespace/hub` — which source. One line per source per invocation. |
| `msgs` | transport messages in the batch. |
| `events` | log records inside them. Higher than `msgs` when records arrive in an envelope. |
| `new` | records not already processed. `events - new` is Event Hubs redelivery. |
| `signals` | `rule()` calls that returned True. |
| `alerts` | matches that also cleared a threshold and weren't already covered. |
| `12ms` | wall clock for the whole batch, including state round-trips and the write. |

**`signals` far exceeding `alerts` is correct** — that gap is thresholds and dedup
working. `signals=0` across every batch means routing, not detection: compare
`/health` → `log_types` against the values in your data.

```kusto
traces
| where message startswith "batch "
| extend hub = extract(@"batch (\S+)", 1, message),
         signals = toint(extract(@"signals=(\d+)", 1, message)),
         alerts  = toint(extract(@"alerts=(\d+)", 1, message)),
         events  = toint(extract(@"events=(\d+)", 1, message))
| summarize events = sum(events), signals = sum(signals), alerts = sum(alerts)
    by hub, bin(timestamp, 5m)
```

That query is throughput, signal rate and alert rate per source, from one line.

---

## What's underneath it

Anything that would otherwise be per-event is **counted during the batch and
reported once**, with the values you need to fix it. Silent when the batch was
clean, which is the normal case.

| Level | Line | Means |
|---|---|---|
| WARNING | `N message(s) were not valid JSON` | A producer is sending something that isn't JSON. |
| INFO | `N event(s) already processed (Event Hubs redelivery)` | Normal in small numbers — at-least-once delivery working as designed. Sustained means checkpointing is failing. |
| WARNING | `N event(s) had no value in the log-type field 'category'` | **The most common misconfiguration.** `log_type_field` names a field your records don't have — usually casing. Fix `config/sources.yaml`. |
| WARNING | `no detections are registered for these log-type values: X (N event(s))` | Routing worked; nothing covers that value. **This is your coverage report.** |
| WARNING | `detection(s) raised and were skipped: My.Rule (5x)` | A broken detection. One traceback per detection per batch, then the count — never five tracebacks for five events. |
| ERROR | `alert storm limit (1000/hour) reached` | A detection is firing far more than expected. Alerts dropped, **signals retained**. |
| ERROR | `... (N record(s) dropped)` | A destination write failed. See below. |

`LOG_LEVEL` controls all of it and applies to the `pyre.*` loggers only, so
changing it never turns the Azure SDKs' logging on or off by accident. `INFO` is
the default and is what produces the batch line.

---

## `/health`

`GET /api/health?code=<function key>`. Configuration, not connections.

```json
{
  "status": "ok",
  "instance": "platform-1",
  "problems": [],
  "state": "redis",
  "detections_source": "blob",
  "destinations": {
    "signal": "blob https://acct.blob.core.windows.net/pyre-output",
    "alert":  "http https://siem.example/alerts (one record per request)"
  },
  "bundle_version": "sha256-f41882eb48167be8",
  "detections": 42,
  "log_types": ["RuntimeAuditLogs", "SignInLogs"],
  "identity": { "endpoint": true, "azure_client_id": null,
                "host_connection_client_ids": null },
  "sources": [ { "id": "platform/applog", "function": "detect_platform_applog",
                 "connection": "EVENTHUB_PLATFORM", "consumer_group": "$Default",
                 "log_type_field": "category" } ]
}
```

Read it in this order:

1. **`problems`** — every setting that contradicts another, in words. Empty is the
   goal. A destination selected with nowhere to send it lives here, and is
   otherwise indistinguishable from a healthy app producing no output.
2. **`identity`** — read this **first** on a 503 mentioning a token.
   `endpoint: false` means the app has no managed identity at all, which breaks
   the bundle, the output blobs and the triggers together and looks like three
   separate faults.
3. **`detections`** — did the bundle load?
4. **`log_types`** — do the values in your data appear here, **exactly**?

| `status` | HTTP | |
|---|---|---|
| `ok` | 200 | Everything consistent. |
| `no-sources-configured` | 503 | No `config/sources.yaml`. Zero triggers exist. |
| `bundle-load-failed` | 503 | `error` names the exception. |
| `no-detections-loaded` | 503 | The bundle loaded and contained nothing usable. |
| `configuration-problems` | 503 | Read `problems`. |

> **What `/health` cannot tell you** is whether the Event Hub listeners
> attached. The listener lives in the Functions host; `/health` runs in the
> Python worker and cannot see it. `status: ok` means every trigger's *config* is
> sound, not that any of them is connected. That check is
> [troubleshooting § Is the trigger actually listening?](troubleshooting.md#is-the-trigger-actually-listening).

`/health` also does not prove the destination is reachable — it reports what was
resolved, not what a write would do.

---

## What to alert on

Four rules, roughly in order of what they cost you when missed.

**1. Records are being dropped.** A write failure never fails the batch (or Event
Hubs would redeliver it and the alert would fire twice), so the *only* place it
appears is Application Insights. This is the one that silently loses data:

```kusto
traces | where message contains "record(s) dropped" | where timestamp > ago(15m)
```

**2. The app stopped processing.** Absence of the batch line is the signal:

```kusto
traces | where message startswith "batch " | summarize last = max(timestamp)
```

Alert when `last` is older than your feed's quiet period. Diagnostic logs are
batched by Azure up to ~5 minutes, so allow for that.

**3. Coverage gaps.** New log types arriving with nothing behind them:

```kusto
traces | where message contains "no detections are registered"
```

Worth a weekly digest rather than a page.

**4. A storm limit was hit.** A detection is firing far beyond expectation, and
alerts are being dropped:

```kusto
traces | where message contains "alert storm limit"
```

Also worth watching: `bundle refresh failed` (a worker is serving a stale
bundle), and `listener` (a trigger that never attached).

---

## Sampling

[`host.json`](../host.json) enables Application Insights sampling with
`excludedTypes: Request`. That caps cost, and it means **a fraction of the batch
lines are dropped** at high volume — fine for rates and trends, not for "did this
exact batch run?". For that, use `requests` (never sampled) or the destination
itself.

---

## What is stored between events, and for how long

All of it is keyed with a TTL — there is no sweeper, the window **is** the key's
expiry.

| State | TTL | Backend |
|---|---|---|
| Redelivery guard, per event id | 1 hour | `STATE_BACKEND` |
| Threshold counter, per detection + dedup group | `DedupPeriodMinutes` | same |
| `unique()` distinct-value set | `DedupPeriodMinutes` | same |
| Open-alert claim | `DedupPeriodMinutes` | same |
| Storm counter, per detection per hour | 1 hour | same |

With `STATE_BACKEND=memory` all of it lives and dies with the worker process:
restarting an app resets every dedup window and threshold. That is a useful
debugging lever ("restart and re-send to see if dedup was holding it") and a
correctness problem across scale-out — two workers count independently and both
can alert. `STATE_BACKEND=redis` is the entire fix.

Event Hub **checkpoints** are separate, host-managed, and live in the
`azure-webjobs-eventhub` container of `AzureWebJobsStorage`. They are keyed on the
Azure function name, which is why `detect_<namespace>_<hub>` must stay stable
across deploys — renaming a source in `sources.yaml` starts it reading from
scratch.
