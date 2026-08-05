# The detections repo (starter)

**Copy this folder into its own git repo.** It has no dependency on the pyre
repo — it is a complete, self-publishing Detections-as-Code repo. Detections live
here, separately from the engine, so changing a rule never redeploys code.

```
dac/
├── publish.py                        build + validate + upload to Blob
├── azure-pipelines.yml               push to main -> published automatically
├── azure-pipelines-publish.yml       one stage per instance you publish to
├── azure-pipelines-publish-steps.yml
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
python publish.py                                                    # build + validate -> dist/
python publish.py --upload https://<account>.blob.core.windows.net   # and publish
```

`--upload` needs `Storage Blob Data Contributor` on that account, as you (VS
Code / Azure sign-in) or as a pipeline's service connection. Without it the two
files land in `dist/` and you upload them in the portal — the zip first, then
`current.json`.

Running workers reload within `DETECTIONS_REFRESH_SECONDS` (default 60). Nothing
is redeployed. Check `/health` on the Function App: `bundle_version` will have
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
CreateAlert: true             # false = record signals, never page
```

### Metadata that travels on the alert

None of these change whether the rule fires. They ride on every alert, so a
responder gets the context **with the page** instead of coming back here to read
the detection.

```yaml
DisplayName: "Repeated Event Hub Authorization Failures"
Description: Three or more failed authorization attempts from one client.
Runbook: >
  Check whether the client IP and identity are expected senders for this
  namespace. Usually a stale key or a lost role assignment - but it looks
  exactly like probing too.
Reference: https://learn.microsoft.com/azure/event-hubs/...
Tags: [Azure, EventHub]
Reports:
  MITRE ATT&CK:
    - "TA0006:T1110"
```

They land as `p_detection_name`, `p_description`, `p_runbook`, `p_reference`,
`p_tags` and `p_reports` — see
[signals-and-alerts.md](../docs/signals-and-alerts.md).

### Optional functions on the `.py`

Each takes `event`:

| Function | Returns | Default if absent |
|---|---|---|
| `title(event)` | the alert headline | the RuleID |
| `dedup(event)` | what groups matches into one alert | the title |
| `severity(event)` | override per event | the YAML `Severity` |
| `alert_context(event)` | a dict attached to the alert | `{}` |
| `unique(event)` | count DISTINCT values instead of matches | off |
| `indicators(event)` | pivot values as `p_any_*` fields | none |

`event` is a dict with two extras: `event.deep_get("a", "b")` walks nested keys
safely, and `event.lookup(table, key)` reads `p_enrichment`.

**`indicators()`** is what makes "everything involving this IP" work across log
types that spell the field differently:

```python
def indicators(event):
    return {
        "ip_addresses": [event.get("ClientIp")],
        "usernames":    [event.get("UserPrincipalName")],
    }
```

Values are normalized to a sorted list of strings and empties are dropped. Pick
key names your destination can index — there is no fixed vocabulary.

### The five things that silently break a bundle

`publish.py` refuses to publish if it can catch them, but know them anyway:

| | |
|---|---|
| The `.py` is not in the same folder as its `.yml` | `Filename:` resolves next to the YAML, by basename only |
| `LogTypes:` doesn't match your data exactly | Case-sensitive. `RuntimeAuditLogs` ≠ `runtimeauditlogs`. Check `log_type_field` in the engine's `config/sources.yaml` and the actual value on your records |
| A missing `RuleID`, `Filename` or `LogTypes` | The engine skips the file without an error |
| A shared helper with no `AnalysisType: global` YAML | Its folder never reaches the import path, so `from x import y` fails and every detection using it is skipped |
| A detection imports a third-party package the Function App doesn't have | The engine would skip it too, but only as a runtime warning discovered whenever someone happens to read the logs. `publish.py` blocks this one before it ships: add the package to the Function App's `requirements.txt`, get it deployed, then republish. |

The folder layout is otherwise entirely up to you — the engine walks the whole
tree.

### The import-safety check needs the Function App's `requirements.txt`

`publish.py` treats a detection's import as safe if it's in the Python
standard library, a global helper in this bundle, or something
`requirements.txt` **one directory above `dac/`** actually installs in the
environment running `publish.py` (name mismatches like `pyyaml` → `yaml` or
`azure-storage-blob` → `azure` are resolved automatically via
`importlib.metadata`, no table to maintain by hand). If that file isn't
there, or isn't installed, the check just prints a notice and skips — which
is expected once this folder is split into its own repo per the top of this
README, since there's no Function App checked out next to it. Run it inside
the pyre repo (or `pip install -r ../requirements.txt` before publishing) to
get the real guarantee.

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
lot), use the pyre repo's:

```powershell
python tools/run_local.py --bundle <this repo> --file real-sample.json
```

## Continuous publishing

`azure-pipelines.yml` is ready to go: point an Azure DevOps pipeline at it and
edit the `- template:` blocks at the bottom with your service connections and
storage account URLs. **Adding another pyre instance to publish to is one more
block** — each gets its own service connection, and any of them can be gated on
an approval check.
