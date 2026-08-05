# pyre

**A detection engine for Azure.** Logs arrive on Event Hubs, pyre routes each
record to the Python detections that cover its log type, runs them, and writes a
**signal** for every match and an **alert** for every match that clears its
threshold and isn't a duplicate.

```
  Event Hub(s)                 Function App "pyre"              Destinations
  ─────────────                ───────────────────              ────────────
  logs-in         ──┐          1. unwrap the message            detections/
  signin-logs     ──┼────────▶ 2. read the source's               current.json
  palo-traffic    ──┘             log_type_field                  bundles/<v>.zip
  ...unlimited                 3. run that log type's                 │
                                  detections                   reads ─┘
                               4. rule() true -> SIGNAL   ──▶  SIGNAL_DESTINATION
                               5. threshold + dedup                 blob | http
                               6. survives  -> ALERT      ──▶  ALERT_DESTINATION
                                                                    blob | http
```

The detections are **not in this repo**. They live in their own git repo which
publishes a versioned bundle to Blob storage; workers reload it within a minute,
with no redeploy. See [dac/](dac/) for a starter you can copy.

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

`--json` prints the exact records a destination receives. `--log INFO` prints the
same per-batch line Azure will show you.

## Guides

| | |
|---|---|
| **[docs/deploying.md](docs/deploying.md)** | Stand up an instance: resources, roles, settings, deploy, and how to prove each half works. |
| **[docs/configuration.md](docs/configuration.md)** | Every app setting, in one table. The reference. |
| **[docs/configuring-destinations.md](docs/configuring-destinations.md)** | Where signals go, where alerts go. Blob, HTTP, auth, and worked settings for each shape. |
| **[docs/adding-a-log-source.md](docs/adding-a-log-source.md)** | Onboard one more namespace or hub. Zero secrets, zero collisions. |
| **[docs/writing-detections.md](docs/writing-detections.md)** | How the engine loads and runs detections. |
| **[dac/README.md](dac/README.md)** | Writing and publishing them. |
| **[docs/signals-and-alerts.md](docs/signals-and-alerts.md)** | The payload contract — every field on both records. |
| **[docs/operations.md](docs/operations.md)** | The one log line per batch, `/health`, and what to alert on. |
| **[docs/troubleshooting.md](docs/troubleshooting.md)** | Deployment 403s, empty function lists, and "why no alerts?". |

## Configuration in one paragraph

**Per source** — [`config/sources.yaml`](config/sources.example.yaml), one block
per Event Hubs namespace, any number of hubs under each. Only `namespace:` and
`hub:` are required; everything else defaults to the shape Azure diagnostic
settings emit.

```yaml
namespaces:
  - namespace: platform            # -> app setting EVENTHUB_PLATFORM
    hubs:
      - hub: logs-in                  # defaults: category / time / records envelope

  - namespace: network             # -> app setting EVENTHUB_NETWORK
    hubs:
      - hub: palo-traffic-in
        log_type_field: dataset
        event_time_field: _time
        envelope_field: ""
```

There is no `connection:` field to type per hub — every hub in a namespace shares
that namespace's one connection automatically, so a typo can't point a hub at the
wrong namespace. Copy [`config/sources.example.yaml`](config/sources.example.yaml)
to `config/sources.yaml`, which is gitignored.

**Per instance** — App settings. **Every "which implementation" decision is a
named value, never a presence check:**

```
DETECTIONS_SOURCE   = blob | local
SIGNAL_DESTINATION  = blob | http | none
ALERT_DESTINATION   = blob | http | none
STATE_BACKEND       = memory | redis
```

Setting a URL never switches a mode, two settings can never disagree about which
wins, and a selector pointed at something unconfigured is a **named entry in
`/health` → `problems`** rather than a silent drop. What an instance is *for* —
a first run writing into a blob you can read, or a production feed into an
external SIEM — is entirely these values.

Full table: [docs/configuration.md](docs/configuration.md).

## The three functions

| Function | Trigger | Purpose |
|---|---|---|
| `detect_<namespace>_<hub>` | Event Hub, batched | The one that matters. One per source in `sources.yaml`, registered in a loop — adding a log source is a config change, not code. |
| `health` | GET | Every setting that contradicts another, which bundle loaded, which log types it covers, where each stream writes, and which app setting each trigger binds with. |
| `ingest` | POST | Feed logs straight in, bypassing Event Hubs. Isolates the detection half when you're working out which half is broken. |

`/health` reports *configuration*, not connections: a trigger it calls healthy
may never have attached to its hub. Whether the app is really listening is a
host-side fact, and the checks for it are in
[troubleshooting § Is the trigger actually listening?](docs/troubleshooting.md#is-the-trigger-actually-listening).

## What's in here

```
function_app.py            the triggers - this repo root IS the Function App root
host.json                  runtime settings (batch size)
requirements.txt           runtime deps; requirements-dev.txt adds pytest
config/
  sources.example.yaml     copy to sources.yaml (gitignored) and edit
pyre_engine/               the engine - never imports azure.functions
  config.py                  app settings + sources.yaml + problems()
  processor.py               the batch loop: route -> rule() -> signal -> alert
  registry.py                detections, indexed by log type
  bundle.py                  where the detection bundle comes from
  state.py                   dedup / thresholds / redelivery guard
  sinks.py                   where signals and alerts go
  records.py                 the signal and alert schemas
  event.py                   the object rule() receives
dac/                       a starter detections repo - copy into its own repo
tools/run_local.py         run the whole engine on your laptop, no Azure
tests/
azure-pipelines*.yml       optional: deploy from Azure DevOps, one stage per instance
```

## Running the tests

```powershell
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -q
```

No Azure, a few seconds. The suite imports `function_app.py` exactly as the
worker does — which catches the "deployed fine, function list is empty" failure
before it reaches a deploy.
