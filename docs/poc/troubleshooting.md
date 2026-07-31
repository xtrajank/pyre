# POC troubleshooting

Main guide: **[README.md](README.md)**. Everything here is portal-based.

Two places answer most questions:

- **`health` function URL** in a browser — what the engine thinks is loaded
- **Function App → Monitoring → Log stream** — what it's doing right now

---

## Contents

- [Verifying the wiring your architect set up](#verifying-the-wiring-your-architect-set-up) ← start here if nothing works at all
- [Logs arrive but no alerts appear](#logs-arrive-but-no-alerts-appear)
- [Records are being dropped, or one message counts as one event](#records-are-being-dropped-or-one-message-counts-as-one-event)
- [`/health` returns 503 bundle-load-failed](#health-returns-503-bundle-load-failed)
- [`"detections": 0` but the bundle uploaded fine](#detections-0-but-the-bundle-uploaded-fine)
- [The bundle won't build](#the-bundle-wont-build)
- [Some detections load, others vanish](#some-detections-load-others-vanish)
- [A rule matches but never alerts](#a-rule-matches-but-never-alerts)
- [Alerts stopped appearing after a restart](#alerts-stopped-appearing-after-a-restart)
- [Sending the same test payload twice does nothing](#sending-the-same-test-payload-twice-does-nothing)
- [The functions list is empty after upload](#the-functions-list-is-empty-after-upload)
- [The Event Hub trigger never fires](#the-event-hub-trigger-never-fires)
- [Nothing in pyre-output, but alerts show in the logs](#nothing-in-pyre-output-but-alerts-show-in-the-logs)
- [health / ingest returns 401 or 403](#health--ingest-returns-401-or-403)
- [Optional: Event Grid instant reload](#optional-event-grid-instant-reload)

---

## Verifying the wiring your architect set up

The main guide assumes the resources and the connections between them already
exist. If nothing works at all, walk this checklist to confirm nothing was
missed. All of it is read-only portal navigation.

### 1. The Function App's identity exists

Portal → **Function App** → **Settings → Identity**.

**System assigned** should be **On**, with an Object (principal) ID shown. If
it's Off, either turn it on here, or the app uses a **User assigned** identity —
check that tab, and if so make sure the `AZURE_CLIENT_ID` app setting holds that
identity's **Client ID**.

### 2. That identity can read and write blobs

**This is the one most likely to be missing**, because you created two new
containers in Step 1 that didn't exist when the wiring was done.

Portal → **Storage account** → **Access Control (IAM)** → **Role assignments**.

Look for the Function App's name with **Storage Blob Data Contributor**. If it
isn't there:

**+ Add → Add role assignment** → *Role*: **Storage Blob Data Contributor** →
*Members*: **Managed identity** → **+ Select members** → *Managed identity*:
**Function App** → pick `pyre` → **Review + assign**.

> Role assignments take **up to 5 minutes** to take effect. A 403 immediately
> after assigning usually just means "wait a bit".

Scope matters: assigned at the **storage account** level it covers every
container, including ones created later. Assigned per-container, the new
`detections` and `pyre-output` containers won't be covered.

### 3. The Event Hub connection settings

Portal → **Function App** → **Settings → Environment variables**.

You should find **one** of these two patterns, not both:

**Connection string:**

| Name | Value shape |
|---|---|
| `EVENTHUB_CONNECTION` | `Endpoint=sb://<ns>.servicebus.windows.net/;SharedAccessKeyName=...;SharedAccessKey=...` |

**Or managed identity:**

| Name | Value |
|---|---|
| `EVENTHUB_CONNECTION__fullyQualifiedNamespace` | `<namespace>.servicebus.windows.net` |
| `EVENTHUB_CONNECTION__credential` | `managedidentity` |

If both patterns are present, delete one — the runtime resolves the plain
`EVENTHUB_CONNECTION` first and you'll get confusing auth errors.

The managed-identity form additionally needs, on the **Event Hubs Namespace** →
**Access Control (IAM)**, the Function App's identity holding **Azure Event Hubs
Data Receiver**.

### 4. EVENTHUB_NAME matches a real hub

Portal → **Event Hubs Namespace** → **Entities → Event Hubs**. The name listed
there must exactly equal the `EVENTHUB_NAME` app setting.

### 5. The storage account the Functions runtime uses

Portal → **Function App** → **Settings → Environment variables** →
`AzureWebJobsStorage` (or `AzureWebJobsStorage__accountName`). This is what holds
`azure-webjobs-hosts` and the Event Hub checkpoints. It's normally the same
storage account you're using for `detections` and `pyre-output`, and it should
already be set — if it's missing the app won't start at all.

### 6. Application Insights is attached

Portal → **Function App** → **Settings → Environment variables** →
`APPLICATIONINSIGHTS_CONNECTION_STRING`. Not required for the POC to work, but
without it **Log stream** is much less useful and you'll be debugging blind.

---

## Logs arrive but no alerts appear

**Read the log stream first.** The engine reports routing failures per batch and
names the actual values it saw, which usually ends the investigation:

```
no detections are registered for these log-type values: ApplicationMetricsLogs (7 event(s)).
A detection's YAML LogTypes must contain the value exactly.
```
→ Routing worked. No detection covers that value. Either add one, or you were
expecting a different category.

```
12 event(s) in this batch had no value in the configured log-type field 'dataset'
- check LOG_TYPE_FIELD against your data
```
→ `LOG_TYPE_FIELD` names a field your records don't have. See below.

Neither message appearing at all means no events reached the engine — that's
[the Event Hub trigger](#the-event-hub-trigger-never-fires), not routing.

### The three settings that must match your data

Compare `/health` against a real message body from **Event Hubs → Data Explorer →
View events**:

| `/health` field | Must equal |
|---|---|
| `log_type_field` | the name of the field on each record that names its type — often `Category` or `category` for Azure diagnostic logs |
| `log_types` | must contain the exact value that field holds. Case-sensitive: `RuntimeAuditLogs` ≠ `runtimeauditlogs` |
| `event_envelope_field` | `records` if messages look like `{"records":[...]}`; empty if each message is a single flat record |

An event with no value in the log-type field is skipped by design, so one
malformed record can't stop a batch — which is why the count is logged rather
than raised.

### Isolating which half is broken

POST one message to `ingest` and look at `signals/<date>.jsonl`:

- **No signal** → routing. This section.
- **Signal but no alert** → threshold or dedup. [See below](#a-rule-matches-but-never-alerts). Not a bug.

---

## Records are being dropped, or one message counts as one event

If a message holds many records but the engine seems to see only one — or none —
the envelope isn't being unwrapped.

Azure diagnostic settings send **many log records per message**, wrapped:

```json
{"records": [ {...}, {...}, {...} ]}
```

`EVENT_ENVELOPE_FIELD` (default `records`) is what expands that into individual
events. Check `/health` shows `"event_envelope_field": "records"`.

Three shapes are handled:

| Message body | Result |
|---|---|
| `{"records":[a, b, c]}` | 3 events (envelope field matched) |
| `[a, b, c]` | 3 events (a plain JSON array — always expanded) |
| `{...}` | 1 event |

If your producer wraps records under a **different** key, set
`EVENT_ENVELOPE_FIELD` to that key. If a message legitimately *is* one event that
happens to contain a `records` list, set `EVENT_ENVELOPE_FIELD` to empty.

Detections are written against the **inner record's** fields — never the
envelope. A `rule()` reading `event.get("records")` is a sign of this
misunderstanding.

---

## `/health` returns 503 bundle-load-failed

The `error` field names the cause.

| Error contains | Meaning | Fix |
|---|---|---|
| `ResourceNotFound` / `BlobNotFound` | No `current.json`, or it points at a zip that isn't there | Re-do Step 4b. Open the `detections` container in Storage browser and confirm both files are there, with the zip under `bundles/` |
| `AuthorizationPermissionMismatch` / `403` | The app's identity lacks **Storage Blob Data Contributor** | [Wiring check 2](#2-that-identity-can-read-and-write-blobs) |
| `ContainerNotFound` | The `detections` container doesn't exist | Step 1 |
| `Invalid URL` / connection errors | `BUNDLE_BLOB_ACCOUNT_URL` is wrong | Must be `https://<account>.blob.core.windows.net` — no trailing slash, no container name |

---

## `"detections": 0` but the bundle uploaded fine

The bundle loaded and contained no usable rules. Causes, most common first:

- **A `.py` isn't in the same folder as its `.yml`.** `Filename:` is resolved
  next to the YAML using only its basename, so `Filename: foo.py` means
  "`foo.py`, in this folder". This is the most common bundle mistake by a wide
  margin.
- **Missing `RuleID`, `Filename` or `LogTypes`.** All three are required; the
  rule is skipped silently without them.
- **`current.json` points at the wrong path.** Its `path` must exactly match
  where you uploaded the zip, `bundles/` prefix included.
- **`Enabled: false`** in the YAML.
- **Every rule failed to import** — see the next section.

`python dac_bundler/bundle.py` catches every one of these except `Enabled: false`
*before* you upload, and exits non-zero. If you bundled by hand, that's the thing
to switch to.

> **A wrapping folder inside the zip is fine.** The engine walks the extracted
> tree recursively, so `my-repo/detections/rule.yml` loads exactly as well as
> `rule.yml`. Nesting is not the problem — a `.py` separated from its `.yml` is.

---

## The bundle won't build

`dac_bundler/bundle.py` refuses to produce a bundle it knows would load nothing.
Each message names the file and the fix.

| Message | Meaning |
|---|---|
| `Filename 'x.py' not found next to it (expected rules/x.py)` | The `.py` isn't in the same folder as its `.yml`. Move it, or correct `Filename:`. Only the basename is used, so a path like `Filename: ../lib/x.py` won't work. |
| `missing required key 'RuleID'` / `'Filename'` / `'LogTypes'` | All three are required on a detection. |
| `unreadable YAML` | A syntax error, or a file saved in an encoding other than UTF-8. |
| `found N file(s) ... but no detections` | `--source` points somewhere with no detections. Point it at the folder holding your `.yml`/`.py` pairs. |
| `AnalysisType global needs Filename` | A helper's YAML must name its `.py`. |

Two things that are *not* errors and don't need fixing:

- **`ignored N non-detection YAML file(s)`** — pipeline configs, schemas and docs
  ride along in the zip harmlessly. The engine skips them the same way.
- **`PyYAML not installed - skipping validation`** — it still bundles, just
  blind. `pip install pyyaml` to get the checks back; they're the main reason to
  use the script.

If your repo has detections you deliberately don't want bundled (deprecated
rules, a scratch folder):

```bash
python dac_bundler/bundle.py --exclude "legacy/**" --exclude "wip/**"
```

---

## Some detections load, others vanish

Expected with a broad bundle like panther-analysis — roughly 770 of its rules
load, and the rest reference helper modules that weren't pulled. Look for
`skipping detection <id> ... ModuleNotFoundError` in the log stream.

The fix is to pull the helper directories those detections import from. In
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

Check `signals/<date>.jsonl` to confirm the match happened. If the signal is
there, it's one of the four above, not a detection bug.

---

## Alerts stopped appearing after a restart

Working as designed, and worth understanding before you demo.

`STATE_BACKEND=memory` keeps dedup counters, thresholds and the redelivery guard
**inside the worker process**. A restart, a scale event or an idle timeout clears
all of it. After a cold start the same events will alert again, because nothing
remembers the earlier alert.

Conversely, re-sending events you already sent produces *nothing* while the
worker is still warm. For a clean demo run, **Restart** the app first (Portal →
Function App → Overview → Restart).

This is the single behaviour that differs from production. Redis fixes it, and
the code is already written — `STATE_BACKEND=redis` plus `REDIS_HOST`.

---

## Sending the same test payload twice does nothing

The redelivery guard. Event Hubs is at-least-once, so a checkpoint retry can
redeliver a batch that was already processed; the processor keys on the transport
event id, or — when there isn't one, as with `ingest` — on a hash of the body. An
identical body is therefore treated as a redelivery.

Change any field (an `eventID`, a timestamp) to send a genuinely new event.

---

## The functions list is empty after upload

`function_app.py` failed to import, so nothing registered. Open **Monitoring →
Log stream** and restart the app to see the error.

| Cause | Fix |
|---|---|
| Dependencies missing or built for the wrong OS | Rebuild with `python tools/poc/package_function.py` — it vendors **Linux** wheels. A zip made by hand from your local `site-packages` will not work. |
| The zip has a wrapping folder | `function_app.py` and `host.json` must be at the **root** of the zip. Check by opening it: if you see `engine/function_app.py`, it's wrong. The script gets this right. |
| Upload didn't finish | Re-upload, then Restart from Overview |
| Python version mismatch | The app must be on Python **3.11**. Portal → Function App → Settings → Configuration → General settings. |

Verify your zip is well-formed before uploading:

```powershell
python -c "import zipfile; z=zipfile.ZipFile('dist/pyre-poc.zip'); print('function_app.py' in z.namelist())"
```

Must print `True`.

---

## The Event Hub trigger never fires

1. **Both connection styles set at once** — see
   [wiring check 3](#3-the-event-hub-connection-settings).
2. **`EVENTHUB_NAME` doesn't match a real hub** —
   [wiring check 4](#4-eventhub_name-matches-a-real-hub).
3. **Managed identity without the role** — needs **Azure Event Hubs Data
   Receiver** on the namespace, and up to 5 minutes to propagate.
4. **The consumer group has already checkpointed past your events.** The trigger
   uses `$Default` and resumes from the last checkpoint, so events sent *before*
   the app first ran may never be delivered. Send fresh ones.
5. **The app is stopped or in an error state** — Portal → Function App →
   Overview → Status.

`ingest` working while `detect` doesn't is a clean signal that the problem is the
Event Hub connection, not the engine.

---

## Nothing in pyre-output, but alerts show in the logs

The dispatch happened; the write didn't.

- `OUTPUT_BLOB_ACCOUNT_URL` unset → the logs say `routed to blob destination
  'blob_alerts' but no blob sink is configured`.
- `DEFAULT_ROUTES` not set to `blob_alerts` → the logs say `routed to
  unknown/disabled destination`.
- The identity lacks write permission → `append-blob write failed` in the logs.
  See [wiring check 2](#2-that-identity-can-read-and-write-blobs).

Blob-write failures are deliberately swallowed rather than raised: losing the
visualisation must not fail the batch, because Event Hubs would redeliver it and
the alert would fire twice. So always check the logs, not just the container.

---

## health / ingest returns 401 or 403

**401** — missing or wrong function key. Get the full URL with the key already in
it: Portal → Function App → Overview → Functions → click the function → **Get
function URL**. Don't hand-assemble it.

**403** — network restrictions. Portal → Function App → **Settings →
Networking** → *Public network access* must be **Enabled** for the HTTP functions
to be reachable. (The production Terraform in `infra/` deliberately disables
this; the POC app should have it on.) Also check **Access restrictions** for an
IP allow-list.

---

## Optional: Event Grid instant reload

You have an Event Grid resource, and there's a clean use for it — but it's
genuinely optional, so skip it if you're short on time. The 30-second poll is
fine for a demo.

By default a worker re-checks the bundle pointer every
`REFRESH_INTERVAL_SECONDS`. Wiring Event Grid turns that poll into a **push**: a
blob write in `detections` fires the `bundle_published` function, which marks the
bundle stale so the very next batch reloads it.

Portal → **Storage account** → **Events** → **+ Event Subscription**:

| Field | Value |
|---|---|
| Name | `pyre-bundle-published` |
| Event Schema | Event Grid Schema |
| System Topic Name | `pyre-storage-topic` (it creates one) |
| Filter to Event Types | **Blob Created** only — uncheck the rest |
| Endpoint Type | **Azure Function** |
| Endpoint | your app → `bundle_published` |

Then on the **Filters** tab, set *Subject Begins With* to:

```
/blobServices/default/containers/detections/
```

so writes to `pyre-output` don't trigger it.

The function only marks the registry stale; it never reloads inline. So a bad
publish can't take detection down — the worker keeps serving the last-good
registry.
