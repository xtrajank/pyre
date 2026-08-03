# pyre

**A detection engine for Azure.** Logs arrive on Event Hubs, pyre routes each
record to the Python detections that cover its log type, runs them, and writes a
**signal** for every match and an **alert** for every match that clears its
threshold and isn't a duplicate.

```
  Event Hub(s)                 Function App "pyre"                Storage
  ─────────────                ───────────────────                ───────
  logs-in         ──┐          1. unwrap the message              detections/
  signin-logs     ──┼────────▶ 2. read the source's                 current.json
  palo-traffic    ──┘             log_type_field                    bundles/<v>.zip
  ...unlimited                 3. run that log type's                  │
                                  detections                    reads ─┘
                               4. rule() true -> SIGNAL
                               5. threshold + dedup                pyre-output/
                               6. survives -> ALERT      writes ──▶  signals/<date>.jsonl
                                                                     alerts/<date>.jsonl
```

The detections are **not in this repo**. They live in their own git repo which
publishes a versioned bundle to Blob storage; workers reload it within a minute,
with no redeploy. See [dac/](dac/) for a starter you can copy.

## Guides

| | |
|---|---|
| **[docs/poc.md](docs/poc.md)** | Stand up the POC in the portal. One hub, one storage account, alerts in a blob you can read. ~30 minutes. |
| **[docs/dev.md](docs/dev.md)** | A second environment where changes get tested before they touch prod. |
| **[docs/prod.md](docs/prod.md)** | All log sources, shared state, a real SIEM destination, and the deploy pipeline. |
| **[docs/adding-a-log-source.md](docs/adding-a-log-source.md)** | Step-by-step: onboard one more namespace or hub, zero secrets, zero collisions. |
| **[docs/troubleshooting.md](docs/troubleshooting.md)** | Deployment 403s, empty function lists, and "why no alerts?". |
| **[dac/README.md](dac/README.md)** | Writing and publishing detections. |

## What's in here

```
function_app.py          the triggers - this repo root IS the Function App root
host.json                runtime settings (batch size)
requirements.txt
config/sources.yaml      every log source, and how to read each one
pyre_engine/             the engine
  config.py                app settings + sources.yaml
  processor.py             the batch loop: route -> rule() -> signal -> alert
  registry.py              detections, indexed by log type
  bundle.py                where the detection bundle comes from
  state.py                 dedup / thresholds / redelivery guard
  sinks.py                 where signals and alerts go
  records.py               what a signal and an alert look like
  event.py                 the object rule() receives
dac/                     a starter detections repo - copy into its own repo
tools/run_local.py       run the whole engine on your laptop, no Azure
tests/
azure-pipelines.yml      optional: deploy from Azure DevOps
```

## Try it right now, with no Azure

```powershell
pip install -r requirements.txt
python tools/run_local.py
```

```
bundle    ...\dac
          1 detection(s) covering ['RuntimeAuditLogs']
routing   'Category' on each record
input     3 message(s) from ...\tools\samples\eventhub_diagnostic.jsonl

SIGNALS  4   (one per rule() that returned True)
    held   Azure.EventHub.AuthFailure          eh-auth-failure:203.0.113.55
    held   Azure.EventHub.AuthFailure          eh-auth-failure:203.0.113.55
  ->alert  Azure.EventHub.AuthFailure          eh-auth-failure:203.0.113.55
    held   Azure.EventHub.AuthFailure          eh-auth-failure:198.51.100.77

ALERTS   1   (matches that also cleared Threshold and dedup)
           [Medium] Event Hub authorization failures from 203.0.113.55 on logs-in
```

Four matches, one alert. That gap — thresholds and dedup — is the difference
between a detection platform and a grep loop, and it is the same code that runs
in Azure. Point it at your own detections and your own logs:

```powershell
python tools/run_local.py --bundle ..\my-detections --file my-logs.json --log-type-field Category
```

## The whole configuration surface

**Per source** — [config/sources.yaml](config/sources.yaml), one block per
Event Hubs namespace, any number of hubs under each. Only `namespace:` and
`hub:` are required:

```yaml
namespaces:
  - namespace: applogns           # -> app setting EVENTHUB_APPLOGNS
    hubs:
      - hub: logs-in                 # defaults: category / time / records envelope

  - namespace: network             # -> app setting EVENTHUB_NETWORK
    hubs:
      - hub: palo-traffic-in
        log_type_field: dataset
        event_time_field: _time
        envelope_field: ""
```

There is no `connection:` field to type per hub. Every hub in a namespace
shares that namespace's one connection automatically, so a typo can't point a
hub at the wrong (or a nonexistent) namespace. See
[docs/adding-a-log-source.md](docs/adding-a-log-source.md) for the full
step-by-step to onboard a new one.

**Per environment** — app settings in the portal:

| Setting | What it does |
|---|---|
| `EVENTHUB_<NAMESPACE>` | Event Hub auth for that namespace - identity-based, never a connection string. One per `namespace:` in `sources.yaml`; see [adding-a-log-source.md](docs/adding-a-log-source.md). |
| `DAC_BLOB_ACCOUNT_URL` | `https://<account>.blob.core.windows.net` holding the published detections. Empty = read `DAC_LOCAL_DIR` off disk. |
| `DAC_CONTAINER` | default `detections` |
| `DAC_REFRESH_SECONDS` | default `60` — how fast a published detection goes live |
| `OUTPUT_BLOB_ACCOUNT_URL` | write signals/alerts to blobs you can read in the portal |
| `OUTPUT_BLOB_CONTAINER` | default `pyre-output` |
| `OUTPUT_HTTP_URL` | POST signals/alerts to a SIEM instead. Wins over the blob. |
| `ALERT_WEBHOOK_URL` | optional: also POST each alert to a case tool |
| `REDIS_HOST` | set it and dedup state is shared across workers (production). Unset = in-process. |
| `STORM_LIMIT` | max alerts per detection per hour, default `1000` |
| `PYRE_ENV` | a label shown by `/health` |

That's all of it. There is no mode switch to get wrong: setting
`DAC_BLOB_ACCOUNT_URL` is what picks Blob, setting `REDIS_HOST` is what picks
shared state.

## The three functions

| Function | Trigger | Purpose |
|---|---|---|
| `detect_<namespace>_<hub>` | Event Hub, batched | The one that matters. One per source in `sources.yaml`. |
| `health` | GET | Which bundle loaded, how many detections, which log types, which field each source routes on. |
| `ingest` | POST | Feed logs straight in, bypassing Event Hubs. Isolates the detection half when you're working out which half is broken. |
