# tools/poc — the bare-bones POC kit

Everything here exists to stand the POC up and prove it works. Nothing here runs
in production. The full walkthrough is **[docs/poc/README.md](../../docs/poc/README.md)**.

| Path | What it is |
|---|---|
| **`dac_bundler/`** | **Drop this folder into your own DaC repo.** Turns it into the two blobs the engine reads, validating first. Has its own [README](dac_bundler/README.md). |
| `dac/` | A throwaway self-contained bundle (3 AWS CloudTrail rules + 1 global helper), for proving the plumbing before your own detections are involved. |
| `samples/cloudtrail_poc.jsonl` | 10 flat events for that bundle: 8 match a rule, 3 raise an alert. |
| `samples/eventhub_diagnostic.jsonl` | 3 Azure Event Hubs `RuntimeAuditLogs` messages in the real `{"records":[...]}` envelope — 7 records total. Use these to check envelope unwrapping. |
| `publish_bundle.py` | Package a detections directory into the two blobs the engine reads. Writes them to `dist/detections/` for a portal upload by default; `--account-url` uploads them directly if you have credentials. |
| `package_function.py` | Build `dist/pyre-poc.zip` with **Linux** dependencies vendored in, so the function can be uploaded by hand with no build step on the Azure side. |
| `read_output.py` | Print what the engine appended to the output container. Needs `az login`; without it, read the blobs in the portal instead. |

Both build scripts need only Python — no Azure CLI, no Core Tools.

## The bundle in `dac/`

Three rules, chosen to exercise a different part of the pipeline each:

| RuleID | Fires on | Shows |
|---|---|---|
| `POC.AWS.Console.RootLogin` | root console sign-in | plain match → alert, and **dedup**: two root logins → 2 signals, 1 alert |
| `POC.AWS.IAM.UserCreated` | `CreateUser` | `alert_context()` carrying detail into the alert |
| `POC.AWS.Console.LoginFailed` | failed console sign-in | **Threshold: 3** — signals below the threshold never alert, and `severity()` computed per event |

`global_helpers/poc_helpers.py` is imported by bare name (`from poc_helpers import ...`)
from all three, so the run also proves the Panther-style global-helper import path
works — the mechanism panther-analysis depends on heavily.

Feeding `samples/cloudtrail_poc.jsonl` through this bundle must produce
**8 signals and 3 alerts**. That is asserted in
[tests/test_poc_backends.py](../../tests/test_poc_backends.py), so if the guide's
expected output ever drifts from reality, the test suite fails.

## Try it with no Azure at all

```bash
# the throwaway bundle, flat events
python tools/testlab/run_local.py --bundle tools/poc/dac --file tools/poc/samples/cloudtrail_poc.jsonl

# the Azure shape: enveloped records, routed on a different field
python tools/testlab/run_local.py --bundle tools/poc/dac_bundler/example \
  --file tools/poc/samples/eventhub_diagnostic.jsonl --log-type-field Category
```

The second is the one that matches the POC's real data: 3 messages carrying 7
records → 4 signals → 1 alert. Point `--bundle` at your own repo to test your
own detections the same way.
