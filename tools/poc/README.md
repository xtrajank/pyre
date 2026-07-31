# tools/poc — the bare-bones POC kit

Everything here exists to stand the POC up and prove it works. Nothing here runs
in production. The full walkthrough is **[docs/poc/README.md](../../docs/poc/README.md)**.

| Path | What it is |
|---|---|
| `dac/` | A small, self-contained detection bundle (3 AWS CloudTrail rules + 1 global helper). Guarantees a green demo without depending on anything external. |
| `samples/cloudtrail_poc.jsonl` | 10 CloudTrail events designed so 8 match a rule but only 3 raise an alert — that gap is the demo. |
| `publish_bundle.py` | Zip a detections directory → upload to Blob → flip the pointer. The POC version of `pyre publish` (publishes an unversioned directory too). |
| `package_function.py` | Build `dist/pyre-poc.zip` with Linux dependencies vendored in, for uploading the function by hand. |
| `read_output.py` | Print what the engine appended to the output container. The "did it work?" check. |

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
python tools/testlab/run_local.py --bundle tools/poc/dac --file tools/poc/samples/cloudtrail_poc.jsonl
```
