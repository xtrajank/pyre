# POC — stand it up in the portal

The goal, stated precisely so you can tell when you're done:

> **A log arriving on the Event Hub is routed to the right detections, `rule()`
> runs, and a `True` produces a signal — and where it clears a threshold, an
> alert — using real `.py` + `.yml` detection files, in our Azure.**

Nothing else is in scope. No Redis, no VNet, no Key Vault, no external service.
Alerts land in a blob you open in the portal.

**No Azure CLI and no Core Tools anywhere in this guide.** Everything in Azure
happens in the portal; the only thing that touches Azure from your machine is
VS Code's deploy and an ordinary HTTPS request.

**About 30 minutes.**

---

## Before you start

You have: an Event Hubs namespace with a hub, a storage account, and an empty
Function App (Linux, Python 3.11), created by your architect. On your laptop:
Python 3.11+, VS Code with the **Azure Functions** extension, and this repo.

**Prove the engine works before touching Azure.** Two seconds, no cloud:

```powershell
pip install -r requirements.txt
python tools/run_local.py
```

You want **4 signals and 1 alert**. If you see that, every problem from here on
is an Azure configuration problem, not a code problem. That is worth knowing up
front.

Keep four portal blades to hand on the Function App: **Overview** (URL,
Restart), **Settings → Environment variables**, **Overview → Functions**, and
**Monitoring → Log stream**.

---

## Step 1 — two blob containers

Storage account → **Data storage → Containers** → **+ Container**, twice, leaving
access level **Private**:

| Container | Holds |
|---|---|
| `detections` | the published detection bundle + `current.json` |
| `pyre-output` | `signals/<date>.jsonl` and `alerts/<date>.jsonl` — what you'll read |

The containers already there (`azure-webjobs-hosts`, `azure-webjobs-secrets`,
`app-package`, `$logs`) belong to the Functions runtime. Leave them alone.

---

## Step 2 — give the Function App access to that storage

The app reads detections and writes output **as itself**, with no keys or
connection strings.

1. Function App → **Settings → Identity** → **System assigned** → **Status: On**
   → Save.
2. Storage account → **Access Control (IAM)** → **+ Add → Add role assignment**
   → role **Storage Blob Data Contributor** → **Managed identity** → your
   Function App → Review + assign.

> Role assignments take **up to 5 minutes** to apply. A 403 in the first few
> minutes is usually just this.

---

## Step 3 — find out what your logs actually look like

**Do not skip this.** Two properties of your data decide two settings, and
getting either wrong produces the same symptom: everything appears to work and
no alerts ever appear.

Event Hubs Namespace → your hub → **Data Explorer → View events** → click an
event → **Body**.

### 3a. Are records wrapped in an envelope?

Azure diagnostic settings — which is what feeds your hub — batch many records
into one message:

```json
{
  "records": [
    { "category": "RuntimeAuditLogs", "ActivityStatus": "Failure", "...": "..." },
    { "category": "RuntimeAuditLogs", "ActivityStatus": "Success", "...": "..." }
  ]
}
```

The engine unwraps this and evaluates **each record as its own event**, so your
detections are written against the *inner* record's fields — never the envelope.
That's `envelope_field: records`, which is the default.

If your body is a single flat object with no wrapping array, set
`envelope_field: ""` in Step 4.

### 3b. Which field routes, and what values does it hold?

`log_type_field` names the field the engine reads off **each record**. It then
runs only the detections whose YAML `LogTypes:` lists that value.

For Azure diagnostic logs that's the record's category — but **check the exact
spelling and casing in your own events**. It is `category` on some resources and
`Category` on others, and the match is case-sensitive.

**Write down every distinct value you see** — `RuntimeAuditLogs`,
`OperationalLogs`, `ApplicationMetricsLogs`, whatever your diagnostic settings
emit. Those strings are what your detections' `LogTypes:` must contain, exactly.

Note the timestamp field too (`time`, `Timestamp`, …).

> Nothing in Data Explorer? No logs are flowing yet. Event Hubs Namespace →
> **Monitoring → Diagnostic settings** → confirm one is enabled and pointed at
> this hub.

---

## Step 4 — point the repo at your hub

Edit [config/sources.yaml](../config/sources.yaml). For a POC it is one entry:

```yaml
sources:
  - hub: logs-in                    # your hub's exact name
    log_type_field: category        # from Step 3b - check the casing
    event_time_field: time          # from Step 3b
    envelope_field: records         # from Step 3a
```

`log_type_field`, `event_time_field` and `envelope_field` can be omitted when
they're already the defaults above. `hub:` cannot.

This file is the whole answer to "how do I add more log sources?" — another
entry, however many you like. Nothing else changes.

---

## Step 5 — app settings

Function App → **Settings → Environment variables → App settings**. Add these
with **+ Add**, then **Apply** at the bottom and confirm the restart:

| Name | Value |
|---|---|
| `PYRE_ENV` | `poc` |
| `DAC_BLOB_ACCOUNT_URL` | `https://<storage-account>.blob.core.windows.net` |
| `DAC_REFRESH_SECONDS` | `30` |
| `OUTPUT_BLOB_ACCOUNT_URL` | `https://<storage-account>.blob.core.windows.net` |
| `AzureFunctionsJobHost__extensions__eventHubs__maxEventBatchSize` | `50` |

The URL is exactly `https://name.blob.core.windows.net` — no trailing slash, no
container.

Then **check** (don't add) that `EVENTHUB_CONNECTION` is already there. It's what
the trigger resolves. It's either a connection string, or — better — the
identity-based trio `EVENTHUB_CONNECTION__fullyQualifiedNamespace`,
`EVENTHUB_CONNECTION__credential` = `managedidentity`. If it's missing, see
[troubleshooting](troubleshooting.md#the-event-hub-trigger-never-fires).

`maxEventBatchSize=50` is deliberately low so a handful of demo events arrive as
one visible batch. It's a **ceiling, not a wait** — small backlogs still deliver
immediately. Production runs 100–256.

---

## Step 6 — deploy the engine

The repo root **is** the Function App root. There is no build and no packaging
step: `function_app.py`, `host.json`, `requirements.txt`, `pyre_engine/` and
`config/` sit at the top level, which is exactly what the worker expects.

1. VS Code → Azure icon in the sidebar → **Sign in to Azure**.
2. Expand your subscription → **Function App** → confirm your app is listed.
3. **Right-click the app in that list** → **Deploy to Function App...** → pick the
   folder (this repo root) → confirm the overwrite prompt.
4. Watch the output pane. It runs a **remote build**, so it finishes with
   dependencies already installed on the worker.

> **Getting a 403, or "failed to fetch" from the portal's zip upload?** That is
> almost always the **SCM site's own access restrictions**, which are separate
> from the ones you set on the app. Allowing your IP on the main site does not
> allow it on `<app>.scm.azurewebsites.net`, and every deploy method goes
> through there.
> **[Fix it here →](troubleshooting.md#deploy-fails-with-403-or-failed-to-fetch)**

### Confirm it landed

Function App → **Overview**, scroll to **Functions**. Expect exactly three:

```
detect_logs_in     Event Hub trigger      (one per source in sources.yaml)
health             HTTP trigger
ingest             HTTP trigger
```

An **empty list** means the app failed to import. Open **Log stream** —
[this section](troubleshooting.md#the-functions-list-is-empty-after-a-successful-deploy)
tells you what to look for.

---

## Step 7 — publish some detections

The detections are a **separate repo**. Copy the [dac/](../dac/) folder out of
this repo into its own git repo (or just work in it in place for the POC), then:

```powershell
cd dac
python publish.py
```

It validates first, and refuses to produce a bundle that would load nothing.
Then it prints the thing you most need:

```
validated 1 detection(s), 1 global helper(s)

LogTypes declared by these detections. An event's log_type_field
must hold one of these EXACTLY, or it will never be routed:
      1  RuntimeAuditLogs
```

**Compare that against the values you wrote down in Step 3b.** If they don't line
up, nothing will ever fire, and here is where you find out — before uploading.

It writes two files:

```
dist/bundles/sha256-1c5ffec782e77d97.zip
dist/current.json          {"version": "sha256-...", "path": "bundles/sha256-....zip"}
```

Upload both — Storage account → **Storage browser → Blob containers →
detections**. **The zip first, the pointer second:**

1. **Upload** → pick the `.zip` → expand **Advanced** → **Upload to folder**:
   `bundles` → Upload.
2. **Upload** → pick `current.json` → leave the folder blank (container root) →
   tick **Overwrite if files already exist** → Upload.

That order is the whole reason publishing is two files: a worker must never be
able to read a pointer to a bundle that isn't there yet.

Old zips are harmless — nothing reads them once the pointer moves on. Keep a
few: they're a one-click rollback.

> Once this works by hand, `python publish.py --upload https://<account>.blob.core.windows.net`
> does both uploads in the right order, and the pipeline in
> [dac/azure-pipelines.yml](../dac/azure-pipelines.yml) does it on every push.

---

## Step 8 — prove it works

### 8a. Is the engine loaded?

Function App → **Overview → Functions → health → Get function URL** → paste in a
browser.

```json
{
  "env": "poc",
  "state": "memory",
  "output": "https://pyrestor.blob.core.windows.net/pyre-output",
  "sources": [
    { "hub": "logs-in", "log_type_field": "category",
      "event_time_field": "time", "envelope_field": "records" }
  ],
  "bundle_version": "sha256-1c5ffec782e77d97",
  "detections": 1,
  "log_types": ["RuntimeAuditLogs"],
  "status": "ok"
}
```

Read it carefully — it answers the two questions behind every "why no alerts?":

- **`detections`** — did the bundle load? `0` means the upload landed but nothing
  in it was a usable detection.
- **`log_types`** — built from your detections' `LogTypes:`. It must contain the
  exact values you saw in Step 3b. If this says `["RuntimeAuditLogs"]` and your
  records carry `"category": "OperationalLogs"`, nothing will ever fire, and
  this line is what tells you.

Also check `sources[].log_type_field` against your data's actual casing.

`503` with `"status": "bundle-load-failed"` means Step 7 didn't land; the `error`
field says why.

### 8b. Send logs straight to the detections

`ingest` runs the identical path as the Event Hub trigger from `process_batch`
onward — it just skips Event Hubs. Use it to test the *detection* half in
isolation, so when something's wrong you know which half is at fault.

Copy a real message body out of Data Explorer (Step 3) into a file, envelope and
all. Then:

```powershell
$url = "<paste the ingest function URL>"
Invoke-RestMethod -Method Post -Uri $url -ContentType "application/json" -InFile my-sample.json
```

```
accepted source
-------- ------
       1 logs-in
```

That's one *message*. If it's an envelope holding 12 records, the engine
evaluates 12 events from it.

> No real data yet?
> [tools/samples/eventhub_diagnostic.jsonl](../tools/samples/eventhub_diagnostic.jsonl)
> is three correctly-shaped `RuntimeAuditLogs` messages — but note they use
> `Category`/`Timestamp` (PascalCase), so they only route if your
> `log_type_field` matches.

### 8c. Read the output

Storage account → **Storage browser → Blob containers → pyre-output**. Click a
file, then the **Edit** tab, for the raw JSON lines.

| Blob | One line per | Answers |
|---|---|---|
| `signals/<date>.jsonl` | `rule()` that returned `True` | did my detection match at all? |
| `alerts/<date>.jsonl` | alert raised | did it survive threshold + dedup? |

**Signals but no alerts is not a failure.** An alert additionally requires the
match to clear the detection's `Threshold` and not duplicate one already open
within `DedupPeriodMinutes`. That gap is the thing worth demonstrating.

Each signal carries `p_alert_id`: the alert it raised or joined, or `null` if it
was held back. So filtering signals on one alert id gives you the matches behind
that alert, and filtering on `null` gives you everything a threshold suppressed.

**Neither signals nor alerts?** Open **Monitoring → Log stream** and re-send. The
engine names the problem:

```
no detections are registered for these log-type values: OperationalLogs (3 event(s)).
A detection's YAML LogTypes must contain the value exactly.
```

```
4 event(s) from hub 'logs-in' had no value in the log-type field 'category'
- check log_type_field in config/sources.yaml against your data
```

The first means routing worked but no detection covers that value. The second
means `log_type_field` is wrong — usually the casing. Both name the real value.

> Re-posting the exact same payload produces **nothing new**. That's the
> redelivery guard: with no transport event id, the body is hashed, and an
> identical body is a duplicate. Change a field to send a genuinely new event.

---

## Step 9 — the real path, through the Event Hub

Everything above bypassed Event Hubs. Now do it properly, still entirely in the
portal.

Your logs are Azure's own diagnostic records about Event Hub activity, so **the
hub feeds itself**: connecting, sending, and failing to authorise all generate
records that flow back in.

Event Hubs Namespace → your hub → **Data Explorer → Send events** → send
anything. That connection and send are themselves audited.

Azure batches diagnostic logs before delivery, so allow **several minutes** (up
to 5). That's Azure's pipeline, not the engine. Watch **Log stream** for the
batch arriving, then re-read the output blobs.

> Dedup state from Step 8b is still warm, so repeating identical activity may
> produce **no new alerts**. That's correct. For a clean run, **Restart** the app
> first.

---

## Changing a detection — the loop worth showing off

1. Edit a rule in the detections repo — lower a `Threshold:` so something that
   was held back now alerts.
2. `python publish.py`
3. Upload the new zip and `current.json`. The version changed automatically —
   it's a hash of the rule contents.
4. Wait 30 seconds. Re-check `/health`: `bundle_version` has changed.
5. Restart the app (to clear dedup state) and send the same logs again.

**The Function App was never redeployed.** That's the whole point of keeping
detections in their own repo.

---

## Demo script, ~5 minutes

1. **Show a detection.** A `.py` and its `.yml` side by side. Ordinary Python,
   ordinary metadata — reviewable in a pull request, versioned in git.
2. **Show it's loaded.** `/health` in a browser: `detections: N`,
   `bundle_version`. Stress that this came from Blob storage, not from the
   deployment — detections and engine ship separately.
3. **Send logs.** Data Explorer → Send events (Step 9).
4. **Show the signals.** The complete audit of everything that matched.
5. **Show the alerts, and the gap.** Fewer lines. Thresholds held back the noise;
   dedup collapsed repeats into one case. This is the part people don't expect.
6. **Change a detection live.** The loop above. **No redeploy of anything.**

Step 6 is the one that lands. Rehearse it, and have both blobs open in tabs
before you start.

---

## What this POC does not prove

Say this before anyone asks — it's the difference between a credible POC and an
oversold one.

| Not proven | Why | What it needs |
|---|---|---|
| Correct state at scale | Dedup and thresholds are per-worker and reset on a cold start. One instance is right; two would double-count. | `REDIS_HOST` — [prod.md](prod.md) |
| Throughput and cost at volume | Ten events at batch size 50 says nothing about millions/hour | A load test, batch size 256 |
| Normalization | It assumes logs already carry a usable log-type field | A normalizer upstream, or per-source config for each feed's real shape |
| Alert delivery | Alerts go to a blob, not a case tool | `ALERT_WEBHOOK_URL` |
| Network isolation | Everything is on public endpoints | VNet + private endpoints |
| Automated publishing | You're uploading two blobs by hand | The pipeline in `dac/azure-pipelines.yml` |

Every one of those is a setting or a pipeline, not an engine change. That's a
genuinely useful thing to be able to say.

**Next:** [dev.md](dev.md), then [prod.md](prod.md).
