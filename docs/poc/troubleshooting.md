# POC troubleshooting

Ordered by how often each one actually bites. Main guide: **[README.md](README.md)**.

Two commands solve most of it:

```bash
curl "https://$APP.azurewebsites.net/api/health?code=$KEY"   # what the engine thinks is loaded
func azure functionapp logstream pyre                        # what it's doing right now
```

---

## Logs arrive but no alerts appear

Nine times out of ten this is the **log type not matching**, and `/health` tells
you immediately:

```json
{ "log_type_field": "dataset", "log_types": ["AWS.CloudTrail"] }
```

The engine reads the field named by `log_type_field` off each event, and runs
only detections whose YAML `LogTypes:` contains that exact value. So check:

1. **Does your event carry that field at all?** If your logs use `log_type` or
   `sourcetype` instead of `dataset`, set `LOG_TYPE_FIELD` to match:
   ```bash
   az functionapp config appsettings set -g $RG -n $APP --settings LOG_TYPE_FIELD=<your-field>
   ```
2. **Does the value match exactly?** `AWS.CloudTrail` ≠ `aws.cloudtrail` ≠
   `AWS_CloudTrail`. The comparison is case-sensitive and exact.
3. **An event with no value in that field is skipped silently** — by design, so a
   malformed line can't stop a batch. Confirm the field is populated.

Fastest way to isolate it: POST one event to `ingest` and check `signals`. A
signal but no alert is a *threshold or dedup* question (below). No signal at all
is a *routing* question (this section).

---

## `/health` returns 503 `bundle-load-failed`

The `error` field names the cause.

| Error contains | Meaning | Fix |
|---|---|---|
| `ResourceNotFound` / `BlobNotFound` | No `current.json`, or it points at a zip that isn't there | Re-run Step 6. Check `az storage blob list --account-name $STORAGE -c detections --auth-mode login -o table` |
| `AuthorizationPermissionMismatch` / `403` | The app's identity lacks **Storage Blob Data Contributor** | Step 3. Wait 5 minutes after assigning — propagation is not instant |
| `ContainerNotFound` | The `detections` container doesn't exist | Step 2 |
| `Invalid URL` / connection errors | `BUNDLE_BLOB_ACCOUNT_URL` is wrong | Must be `https://<account>.blob.core.windows.net`, no trailing path |

---

## `"detections": 0` but the bundle published fine

The bundle loaded and contained no usable rules. Causes:

- **The zip has a wrapping folder.** The `.py`/`.yml` pairs must be at the
  *root* of the zip, not inside `dac/`. `publish_bundle.py` gets this right; a
  hand-made zip often doesn't. Check by downloading it and looking.
- **Every rule failed to import.** A detection whose imports don't resolve is
  skipped and logged — check the log stream for `skipping detection`. Usually a
  missing global helper (see below).
- **`Enabled: false`** in the YAML, or `ENABLED_DETECTION_IDS` set to a list that
  excludes them.
- **Missing `RuleID` or `Filename`.** Both are required; the rule is skipped
  without them. `python cli/pyre validate` catches this before you publish.

---

## Some detections load, others vanish

Expected with a broad bundle like panther-analysis — roughly 770 of its rules
load, and the rest reference helper modules that weren't pulled. Look for
`skipping detection <id> ... ModuleNotFoundError` in the logs.

The fix is to pull the helper directories the detections import from. In
`config/detections.yaml`:

```yaml
dac:
  global_helpers: [global_helpers]      # add any other dir your rules import from
```

Then `pyre pull` again and re-publish. A detection that can't import is always
skipped rather than allowed to break the bundle — one bad rule never stops the
other 769.

---

## A rule matches but never alerts

The rule is working; the alert is being suppressed on purpose. In order:

1. **Threshold.** `Threshold: 3` in the YAML means three matches sharing a dedup
   string inside the window before anything fires. Two matches = two signals,
   zero alerts. This is `POC.AWS.Console.LoginFailed` in the sample data.
2. **Dedup.** An alert already open for that dedup string groups the new match
   instead of raising again, for `DedupPeriodMinutes`. Two root logins → one
   alert.
3. **`CreateAlert: false`** in the YAML — signals only, never alerts.
4. **Storm limit.** More than `STORM_LIMIT` alerts for one detection in an hour;
   look for `storm limit hit` in the logs. Unlikely in a demo.

Check `signals/<date>.jsonl` to confirm the match happened — if the signal is
there, it's one of the four above, not a detection bug.

---

## Alerts stopped appearing after a restart / redeploy

Working as designed, and worth understanding before you demo.

`STATE_BACKEND=memory` keeps dedup counters, thresholds and the redelivery guard
**inside the worker process**. A restart, a scale event or an idle timeout clears
all of it. After a cold start the same events will alert again, because nothing
remembers the earlier alert.

Conversely, re-sending events you already sent produces *nothing* while the
worker is still warm. To get a clean demo run:

```bash
az functionapp restart -g $RG -n $APP     # clears in-memory state
```

This is the single behaviour that differs from production. Redis fixes it, and
the code for that is already written — `STATE_BACKEND=redis` plus `REDIS_HOST`.

---

## Sending the same test payload twice does nothing

The redelivery guard. Event Hubs is at-least-once, so a checkpoint retry can
redeliver a batch that was already processed; the processor keys on the
transport event id, or — when there isn't one, as with `ingest` — on a hash of
the body. An identical body is therefore treated as a redelivery.

Change any field (an `eventID`, a timestamp) to send a genuinely new event.

---

## `ingest` / `health` returns 401

Missing or wrong function key. Keys are per-function:

```bash
az functionapp function keys list -g $RG -n $APP --function-name health --query default -o tsv
```

Pass it as `?code=<key>` or an `x-functions-key` header. If the app has
`public_network_access_enabled = false` (the production Terraform sets this), the
HTTP endpoints aren't reachable at all from outside — the POC app should have
public access on.

---

## `func azure functionapp publish` fails

| Symptom | Cause | Fix |
|---|---|---|
| `Can't find app with name "pyre"` | Wrong subscription | `az account set --subscription <id>` |
| Hangs, then a remote-build error | Sync/build timeout | Re-run it; if it persists use `python tools/poc/package_function.py` |
| `ModuleNotFoundError` at runtime, deploy succeeded | Package built for the wrong platform | Deploy with Core Tools (remote build), not a locally-built zip |
| Deploy succeeds, functions list is empty | `function_app.py` failed to import | Check the log stream — usually a syntax error or a missing dependency in `requirements.txt` |

Always run `func azure functionapp publish` from **inside `engine/`** — that
directory is the app root.

---

## The Event Hub trigger never fires

1. **Both connection styles set at once.** Set `EVENTHUB_CONNECTION` *or* the two
   `EVENTHUB_CONNECTION__*` settings, never both.
2. **`EVENTHUB_NAME` doesn't match a real hub.**
   `az eventhubs eventhub list -g $RG --namespace-name $EHNS --query "[].name" -o tsv`
3. **Managed identity without the role.** Needs **Azure Event Hubs Data
   Receiver** on the namespace, and up to 5 minutes to propagate.
4. **Consumer group already checkpointed past your events.** The trigger uses
   `$Default` and starts from the last checkpoint — events sent *before* the app
   first ran may never be delivered. Send fresh ones.
5. **The app is stopped.** `az functionapp show -g $RG -n $APP --query state -o tsv`

---

## Nothing in `pyre-output`, but alerts show in the logs

The dispatch happened; the write didn't.

- `OUTPUT_BLOB_ACCOUNT_URL` unset → the logs say `routed to blob destination
  'blob_alerts' but no blob sink is configured`.
- `DEFAULT_ROUTES` not set to `blob_alerts` → the logs say `routed to
  unknown/disabled destination`.
- The identity lacks write permission → `append-blob write failed` in the logs.

Blob-write failures are deliberately swallowed rather than raised: losing the
visualisation must not fail the batch, because Event Hubs would redeliver it and
the alert would fire twice. So always check the logs, not just the container.

---

## Non-ASCII characters break a local `pyre` command

Fixed — all YAML reads are explicitly UTF-8. If you see a `UnicodeDecodeError`
from `yaml`, you're on an older checkout of this branch; pull again.
