# The detections repo (starter)

**Copy this folder into its own git repo.** It has no dependency on the pyre
repo — it is a complete, self-publishing Detections-as-Code repo. Detections live
here, separately from the engine, so changing a rule never redeploys code.

```
dac/
├── publish.py                  build + validate + upload to Blob
├── azure-pipelines.yml         push to main -> published automatically
├── detections/
│   └── azure_eventhub/
│       ├── eventhub_auth_failure.yml     metadata
│       └── eventhub_auth_failure.py      the rule() logic, SAME folder
└── global_helpers/
    ├── pyre_helpers.yml        AnalysisType: global
    └── pyre_helpers.py         importable by bare name from any detection
```

## Publish

```bash
python publish.py                                                # build + validate -> dist/
python publish.py --upload https://<account>.blob.core.windows.net   # and publish
```

`--upload` needs `Storage Blob Data Contributor` on that account, as you (VS
Code / Azure sign-in) or as a pipeline's service connection. Without it the two
files land in `dist/` and you upload them in the portal — the zip first, then
`current.json`.

Running workers reload within `DAC_REFRESH_SECONDS` (default 60). Nothing is
redeployed. Check `/health` on the Function App: `bundle_version` will have
changed.

## Writing a detection

A detection is **a `.yml` and a `.py` in the same folder**. Only `rule()` is
required:

```python
def rule(event):
    return event.get("ActivityStatus") == "Failure"
```

```yaml
AnalysisType: rule
Filename: my_rule.py          # must sit next to this file
RuleID: "My.Rule.Id"          # unique
Enabled: true
Severity: Medium
LogTypes:
  - RuntimeAuditLogs          # must match your data EXACTLY (case-sensitive)
Threshold: 3                  # matches before an alert fires; 1 = first match
DedupPeriodMinutes: 60
```

Optional functions on the `.py`, each taking `event`:

| Function | Returns | Default if absent |
|---|---|---|
| `title(event)` | the alert headline | the RuleID |
| `dedup(event)` | what groups matches into one alert | the title |
| `severity(event)` | override per event | the YAML `Severity` |
| `alert_context(event)` | a dict attached to the alert | `{}` |
| `unique(event)` | count DISTINCT values instead of matches | off |

`event` is a dict with two extras: `event.deep_get("a", "b")` walks nested keys
safely, and `event.lookup(table, key)` reads `p_enrichment`.

### The four things that silently break a bundle

`publish.py` refuses to publish if it can catch them, but know them anyway:

| | |
|---|---|
| The `.py` is not in the same folder as its `.yml` | `Filename:` resolves next to the YAML, by basename only |
| `LogTypes:` doesn't match your data exactly | Case-sensitive. `RuntimeAuditLogs` ≠ `runtimeauditlogs`. Check `log_type_field` in the engine's `config/sources.yaml` and the actual value on your records |
| A missing `RuleID`, `Filename` or `LogTypes` | The engine skips the file without an error |
| A shared helper with no `AnalysisType: global` YAML | Its folder never reaches the import path, so `from x import y` fails and every detection using it is skipped |

The folder layout is otherwise entirely up to you — the engine walks the whole
tree.

## Testing a detection before you publish

Plain pytest, against the function directly:

```python
import importlib.util, sys
sys.path.insert(0, "global_helpers")          # so `from pyre_helpers import ...` resolves
spec = importlib.util.spec_from_file_location(
    "d", "detections/azure_eventhub/eventhub_auth_failure.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

def test_public_failure_matches():
    assert m.rule({"ActivityStatus": "Failure", "ClientIp": "203.0.113.5"}) is True

def test_internal_failure_ignored():
    assert m.rule({"ActivityStatus": "Failure", "ClientIp": "10.0.0.5"}) is False
```

Put those in `tests/` — the bundler excludes that folder, so they never ship.

To run the whole engine over a real log sample (routing, thresholds, dedup, the
lot), use the pyre repo's `python tools/run_local.py --bundle <this repo>`.

## Continuous publishing

`azure-pipelines.yml` is ready to go: point an Azure DevOps pipeline at it, set
the two account URLs and the service connection name at the top, and every push
to `main` validates, publishes to dev, then waits for approval before prod.
