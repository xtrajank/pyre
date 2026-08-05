# Writing detections

Detections are **not in this repo**. They live in their own git repo, which
publishes a versioned bundle to Blob storage; running workers pick it up within
`DETECTIONS_REFRESH_SECONDS` with no redeploy of anything.

> **[dac/README.md](../dac/README.md) is the full guide** — the YAML keys, every
> optional function, publishing, testing, and the four things that silently break
> a bundle. [`dac/`](../dac/) is a working starter you copy into its own repo.

This page covers how the engine consumes what that repo produces.

---

## A detection is a `.yml` and a `.py` in the same folder

The layout is panther-analysis' own, so an existing detection repo loads
unchanged.

```
detections/azure_eventhub/
├── eventhub_auth_failure.yml     metadata
└── eventhub_auth_failure.py      rule() and friends
```

`Filename:` in the YAML resolves **by basename, next to the YAML**. A `.py` in a
different folder is the single most common way to publish a bundle that uploads
cleanly and registers nothing — `publish.py` fails the build on it.

---

## How a log type reaches a detection

```
record  --[ the source's log_type_field ]-->  a value
value   --[ a detection's LogTypes: ]------>  that detection runs
```

The match is an **exact, case-sensitive string comparison**. `RuntimeAuditLogs`
is not `runtimeauditlogs`. This is the routing table:

| Where | What |
|---|---|
| `config/sources.yaml` → `log_type_field:` | which field on the record holds the value |
| the detection's `LogTypes:` | which values it covers |
| `/health` → `log_types` | every value that actually loaded |

An event whose log type has no detection behind it is counted and named once per
batch:

```
platform/applog: no detections are registered for these log-type values:
OperationalLogs (3 event(s)). A detection's YAML LogTypes must contain the value exactly.
```

**That line is a coverage report.** See
[operations.md](operations.md#what-to-alert-on).

Detections are indexed by log type at load, so an event only ever runs the
detections registered for its own type — which is what keeps hundreds of them
cheap.

---

## What the engine does with each function

| | Called | Used for |
|---|---|---|
| `rule(event)` | every event of that log type | **required.** True writes a signal. |
| `title(event)` | on alert | `p_title`; also the dedup fallback |
| `dedup(event)` | on match | the group matches count toward, and `p_dedup` |
| `severity(event)` | on match | `p_severity` on both records |
| `alert_context(event)` | on alert | `p_context` |
| `unique(event)` | on match | switches `Threshold:` to counting DISTINCT values |
| `indicators(event)` | on match | `p_any_*` pivot fields on both records |

`Threshold:` and `DedupPeriodMinutes:` are what turn matches into alerts: N
matches sharing a dedup string within the window raise one alert, and everything
after that in the window joins it. See
[signals-and-alerts.md](signals-and-alerts.md).

---

## A detection that raises is isolated

One broken detection cannot take the batch, or the bundle, or any other
detection. It is skipped **for that event**, logged once per batch with a count,
and everything else runs:

```
platform/applog: detection(s) raised and were skipped for those events: My.Rule (5x)
```

Likewise a detection that won't **import** — a syntax error, a missing helper —
is skipped at bundle load with a warning, and the rest of the bundle still loads.
`/health` → `detections` is the count that actually registered; compare it to
what `publish.py` reported.

---

## Global helpers

A `.py` paired with an `AnalysisType: global` YAML puts its folder on the import
path, so any detection can `from pyre_helpers import internal_ip` by bare name at
any depth. A helper with **no** paired YAML never reaches the path, and every
detection importing it is silently skipped.

Helper modules are evicted from `sys.modules` on each bundle reload, so editing a
helper takes effect the same way editing a rule does.

---

## Testing before you publish

The fastest loop is the whole engine on your laptop — same processor, registry,
thresholds and dedup that run in Azure, no Azure at all:

```powershell
python tools/run_local.py --bundle ..\my-detections --file real-sample.json `
  --log-type-field category --event-time-field time
```

```
SIGNALS  4   (one per rule() that returned True)
    held   Azure.EventHub.AuthFailure          eh-auth-failure:203.0.113.55
  ->alert  Azure.EventHub.AuthFailure          eh-auth-failure:203.0.113.55

ALERTS   1   (matches that also cleared Threshold and dedup)
           [Medium] Event Hub authorization failures from 203.0.113.55 on logs-in
```

`--json` emits the exact records a destination would receive. `--log INFO` shows
the same per-batch line Azure will show you.

The three field flags mean exactly what the same-named keys in `sources.yaml`
mean — getting them right here is what makes them right there.

For unit tests over `rule()` alone, see
[dac/README.md § Testing](../dac/README.md#testing-a-detection-before-you-publish).

---

## Publishing, and what "live" means

```bash
python publish.py --upload https://<account>.blob.core.windows.net
```

`publish.py` validates every detection, refuses to publish a bundle that would
load nothing, hashes the contents into a version, and uploads **the zip first,
then the pointer** — so a worker reading mid-publish never sees a pointer
referencing a zip that isn't there yet.

Workers reload when, and only when, the version in `current.json` changes, at
most once per `DETECTIONS_REFRESH_SECONDS`. The swap is a single reference
assignment: an in-flight batch finishes on the old bundle and the next one is
live on the new.

**A reload that fails keeps serving the last-good bundle.** A blob or network
blip must never stop detection. Check `/health` → `bundle_version`, and App
Insights for `bundle refresh failed`.
