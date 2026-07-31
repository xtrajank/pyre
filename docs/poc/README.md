# pyre POC — setup guide (Azure Portal)

The goal of this POC is narrow and worth stating precisely:

> **Prove that a log arriving on an Event Hub is routed to the right
> Detections-as-Code rules, evaluated by `rule()`, and that a `True` produces an
> alert — using the real `.py` + `.yml` DaC files, in the company's Azure.**

Everything that isn't needed for that sentence has been left out: no Redis, no
Cribl, no Torq, no VNet, no Key Vault, no external communication of any kind.
Alerts land in a blob you can open and read in the portal.

## What you're doing, and what you're not

The resources and the wiring between them **already exist** — your architect
created them. Your job is five things:

1. Create two blob containers
2. Confirm what your log records actually look like
3. Set the app settings that tell the engine how to behave
4. Build the function zip locally and upload it
5. Bundle your DaC repo and upload it

**No Azure CLI and no Core Tools are needed anywhere in this guide.** Everything
in Azure happens in the portal; everything local is plain Python. The only thing
you run against Azure from your machine is an ordinary HTTPS request, using the
function key you copy out of the portal.

If something doesn't work, the wiring your architect set up is the first thing to
rule out — **[troubleshooting.md](troubleshooting.md)** has a checklist for
verifying every connection, so you can confirm nothing was missed.

**Time: about 30 minutes.**

---

## Contents

1. [What you're building](#1-what-youre-building)
2. [What changed in the code, and why](#2-what-changed-in-the-code-and-why)
3. [Before you start](#3-before-you-start)
4. [Step 1 — create the two blob containers](#step-1--create-the-two-blob-containers)
5. [Step 2 — find out what your logs actually look like](#step-2--find-out-what-your-logs-actually-look-like)
6. [Step 3 — app settings](#step-3--app-settings)
7. [Step 4 — build and deploy the function](#step-4--build-and-deploy-the-function)
8. [Step 5 — bundle and upload your detections](#step-5--bundle-and-upload-your-detections)
   - [**Exactly what the detections container must contain**](#exactly-what-the-detections-container-must-contain) ← the spec
9. [Step 6 — prove it works](#step-6--prove-it-works)
10. [Step 7 — the real path: through the Event Hub](#step-7--the-real-path-through-the-event-hub)
11. [Changing a detection](#changing-a-detection)
12. [Demo script](#demo-script)
13. [What this POC does not prove](#what-this-poc-does-not-prove)
14. [App settings reference](#app-settings-reference)

Afterwards: **[to-production.md](to-production.md)** — the exact POC→production
diff (spoiler: no engine code and no bundler code). Then
**[continuous-deployment.md](continuous-deployment.md)** if you want push-to-main
to deploy itself.

---

## 1. What you're building

```
  you / a log source
        │
        │  JSON log lines
        ▼
  ┌───────────────┐         ┌──────────────────────────────────────────┐
  │  Event Hub    │ batch   │  Function App "pyre"                     │
  │  (1 hub)      ├────────▶│                                          │
  └───────────────┘ trigger │   detect()                               │
                            │     1. read LOG_TYPE_FIELD off the event │
                            │     2. pick that log type's detections   │
                            │     3. run rule()                        │
                            │     4. True → write a SIGNAL             │
                            │     5. threshold + dedup (in-memory)     │
                            │     6. survives → ALERT                  │
                            └───────┬─────────────────────┬────────────┘
                                    │                     │
              reads detections from │                     │ appends alerts to
                                    ▼                     ▼
                    ┌───────────────────────┐  ┌────────────────────────────┐
                    │ Storage: detections/  │  │ Storage: pyre-output/      │
                    │  bundles/<ver>.zip    │  │  alerts/<date>.jsonl   ◀── │
                    │  current.json         │  │  signals/<date>.jsonl      │
                    └───────────────────────┘  └────────────────────────────┘
                              ▲                       the demo artifact
              you upload this │
              from the portal │
```

**Signals vs alerts** — the distinction the demo hinges on. A *signal* is written
every time `rule()` returns `True`: a complete audit of everything that matched.
An *alert* is only raised when that match also clears the detection's `Threshold`
and isn't a duplicate of one already open. Expect the alerts blob to hold far
fewer lines than the signals blob — that gap is thresholds and dedup doing their
job, not events going missing.

---

## 2. What changed in the code, and why

Two production dependencies don't exist in the POC, so each got a drop-in
substitute behind the interface that was already there. **The detection path
itself — routing, `rule()`, signals, thresholds, dedup — is completely
unmodified**, which is the point: what you demo is the real engine.

| Missing | Substitute | Where |
|---|---|---|
| **Redis** (dedup / thresholds / `unique()` / storm limit / redelivery guard) | An in-process store speaking the same commands, behind the same `StateStore`. The key names and TTL rules are shared code, so the two backends can't drift. | [backends/memory_state.py](../../engine/pyre_engine/backends/memory_state.py) |
| **Cribl lake + Torq** (where signals and alerts go) | **Append blobs** — one JSON object per line, appended to the end, readable in the portal. Append is a single server-side operation, so concurrent workers can't clobber each other. | [backends/blob_sink.py](../../engine/pyre_engine/backends/blob_sink.py) |

Both are selected by app settings (`STATE_BACKEND`, `SIGNALS_SINK_URL` /
`OUTPUT_BLOB_ACCOUNT_URL`); point them at Redis and Cribl and the production path
runs instead, with no code change. That is the honest trade of the in-memory
store, stated plainly:

> Dedup windows, thresholds and the redelivery guard live **inside one worker
> process** and reset on a cold start. With one instance and a short demo that
> behaves identically to Redis. It is not correct across scale-out, and it is
> not production. Flip `STATE_BACKEND=redis` when the resource exists — nothing
> else changes.

Both live in [engine/pyre_engine/backends/](../../engine/pyre_engine/backends/),
which is the **only** place in the engine that knows which environment it's in.
Everything above it — routing, `rule()`, signals, thresholds, dedup, dispatch —
is identical in the POC and in production. That's what makes this a proof of the
real thing rather than a mock-up, and it means going to production is a
configuration change: **[to-production.md](to-production.md)** lists the exact
diff.

The Function App also gained three small functions beside `detect`, all of them
there to make the POC demonstrable and debuggable without a CLI — `health`,
`ingest`, `bundle_published`. See [the reference](#app-settings-reference).

---

## 3. Before you start

On your machine you need **Python 3.11 or 3.12** and this repo. That's it.

```powershell
python --version
pip install -r engine/requirements.txt
```

Prove the engine works before you touch Azure — this runs the real processor with
no cloud at all, and takes about two seconds:

```powershell
python tools/testlab/run_local.py --bundle tools/poc/dac --file tools/poc/samples/cloudtrail_poc.jsonl
```

You should see **8 signals and 3 alerts**. Those are throwaway sample detections,
not yours — the point is only that if you see that number, every problem from
here on is an Azure configuration problem rather than a code problem. That's
worth knowing up front.

Once you have your own detections and a real log sample, run the same check
against them — this is the fastest loop there is, and it uses the same engine:

```powershell
python tools/testlab/run_local.py `
  --bundle <path-to-your-dac-repo> `
  --file my-sample.json `
  --log-type-field Category
```

It prints which field it routed on and which log types your bundle covers, so a
mismatch shows up in one line before Azure is involved at all.

In the portal, open your Function App and keep these four blades handy:

- **Overview** — the app's URL, and Restart
- **Settings → Environment variables** (older portals: *Configuration*)
- **Overview → Functions** — the function list and their keys
- **Monitoring → Log stream** — live logs

---

## Step 1 — create the two blob containers

Portal → your **Storage account** → **Data storage → Containers** → **+ Container**.

Create both, leaving *Public access level* at **Private**:

| Container | Holds |
|---|---|
| `detections` | the published DaC bundle + the `current.json` version pointer |
| `pyre-output` | `alerts/<date>.jsonl` and `signals/<date>.jsonl` — the demo artifact |

That's the whole step. The four containers already there (`$logs`,
`app-package`, `azure-webjobs-hosts`, `azure-webjobs-secrets`) belong to the
Functions runtime — leave them alone.

> The engine creates `pyre-output` itself if it's missing, so that one is belt
> and braces. `detections` must exist before Step 5.

---

## Step 2 — find out what your logs actually look like

**Do not skip this.** Two properties of your data decide two app settings, and
getting either wrong produces the same symptom: everything appears to work and
no alerts ever appear.

Portal → **Event Hubs Namespace** → your hub → **Data Explorer** → **View
events** → click any event and look at its **Body**.

### 2a. Are your records wrapped in an envelope?

Azure diagnostic settings — which is what's feeding your hub, since these are
the platform's own logs about Event Hub activity — do **not** send one log per
message. They batch many records into one message wrapped in a `records` array:

```json
{
  "records": [
    { "Category": "RuntimeAuditLogs", "ActivityName": "Authorization", "ActivityStatus": "Failure", ... },
    { "Category": "RuntimeAuditLogs", "ActivityName": "ConnectionOpen", "ActivityStatus": "Success", ... }
  ]
}
```

The engine unwraps this and evaluates **each record as its own event**, so your
detections are written against the *inner* record's fields — never the envelope.
That behaviour is the `EVENT_ENVELOPE_FIELD` setting, and it defaults to
`records`, which is what you want here.

If your body is a single flat object with no wrapping array, set
`EVENT_ENVELOPE_FIELD` to empty instead.

### 2b. Which field routes to detections, and what values does it hold?

This is the setting you were reaching for. `LOG_TYPE_FIELD` names the field the
engine reads off **each record**; it then runs only the detections whose YAML
`LogTypes:` lists that value.

For Azure diagnostic logs that field is the record's category. **Check the exact
spelling and casing in your own events** — it is `Category` on some resources and
`category` on others, and the match is case-sensitive:

| What you see in the record | What to set |
|---|---|
| `"Category": "RuntimeAuditLogs"` | `LOG_TYPE_FIELD` = `Category` |
| `"category": "RuntimeAuditLogs"` | `LOG_TYPE_FIELD` = `category` |

**Write down every distinct value you see** — `RuntimeAuditLogs`,
`ApplicationMetricsLogs`, `OperationalLogs`, whatever your diagnostic settings
emit. Those strings are what your detections' `LogTypes:` must contain, exactly.

Also note the field carrying the record's own timestamp (`Timestamp`, `time`, …)
for `EVENT_TIME_FIELD`.

> If Data Explorer shows nothing, no logs are flowing yet. Confirm the
> diagnostic setting is enabled and pointed at this hub:
> **Event Hubs Namespace → Monitoring → Diagnostic settings**.

You don't have to get this perfect now — [Step 6](#step-6--prove-it-works) shows
you how the engine reports a mismatch, naming the exact values it saw.

---

## Step 3 — app settings

Portal → **Function App** → **Settings → Environment variables** → **App
settings** tab. Add each of these with **+ Add**, then click **Apply** at the
bottom and confirm the restart.

Three of these come straight from what you found in Step 2 — they're marked
**(from Step 2)**.

| Name | Value |
|---|---|
| `PYRE_ENV` | `poc` |
| `STATE_BACKEND` | `memory` |
| `LOG_TYPE_FIELD` | **(from Step 2)** — e.g. `Category` |
| `EVENT_TIME_FIELD` | **(from Step 2)** — e.g. `Timestamp` |
| `EVENT_ENVELOPE_FIELD` | **(from Step 2)** — `records` for Azure diagnostic logs |
| `BUNDLE_MODE` | `blob` |
| `BUNDLE_BLOB_ACCOUNT_URL` | `https://<storage-account>.blob.core.windows.net` |
| `REFRESH_INTERVAL_SECONDS` | `30` |
| `OUTPUT_BLOB_ACCOUNT_URL` | `https://<storage-account>.blob.core.windows.net` |
| `OUTPUT_BLOB_CONTAINER` | `pyre-output` |
| `DEFAULT_ROUTES` | *(leave empty)* |
| `AzureFunctionsJobHost__extensions__eventHubs__maxEventBatchSize` | `50` |

Replace `<storage-account>` with the real name — the URL is exactly
`https://name.blob.core.windows.net`, no trailing slash and no container.

Then **check** (don't add) that `EVENTHUB_NAME` is already there and matches the
hub your architect created. It's what the trigger resolves — if it's missing, add
it with the hub's name. The Event Hub *connection* settings should also already
be present; [troubleshooting.md](troubleshooting.md#the-event-hub-trigger-never-fires)
tells you what they should look like if you want to verify.

`maxEventBatchSize=50` is deliberately low so a handful of demo events arrive as
one visible batch. It is a **ceiling, not a wait** — small backlogs are still
delivered immediately, so this doesn't delay anything. Production runs 256+.

---

## Step 4 — build and deploy the function

### 4a. Stage the app root

```powershell
python tools/poc/package_function.py --stage
```

This writes `dist/functionapp/` — a **complete Function App root**: your engine
code plus the `config/` folder the runtime reads. `engine/` on its own isn't
quite it, because `config/` lives beside it in the repo; staging puts them
together so one artifact works for VS Code, a manual upload, and CI/CD alike.

It stays small (no vendored dependencies) because everything below builds them on
the Linux worker from `requirements.txt` — which is both faster to upload and
guaranteed to match the runtime.

### 4b. Deploy it from VS Code

You said the zip upload is failing, and this is the better route anyway:

1. Install the **Azure Functions** extension if you haven't, and sign in
   (Azure icon in the sidebar → **Sign in to Azure**).
2. In the Azure panel, expand your subscription → **Function App** → confirm
   `pyre` is listed.
3. In the **Explorer**, right-click `dist/functionapp` → **Deploy to Function
   App...** → pick `pyre` → confirm the overwrite prompt.
4. Watch the output pane. On Linux it runs a **remote build**, so the deploy
   finishes with dependencies already installed.

This is a legitimate way to run the POC — not a workaround. Deploying from VS
Code is the same zip-deploy API a pipeline uses, just triggered by hand, so
moving to CI/CD later changes the trigger and nothing else.

> **Why the portal upload probably failed.** Kudu's Zip Push Deploy authenticates
> with **SCM basic auth**, which many enterprise tenants disable by policy
> (Function App → Settings → Configuration → *SCM Basic Auth Publishing
> Credentials* = Off). VS Code doesn't use it — it authenticates with your Azure
> AD identity through ARM — so it works where the portal upload returns 401.
> Flex Consumption has no Kudu at all, which produces the same symptom.
> [More causes →](troubleshooting.md#the-zip-upload-fails)

<details>
<summary><strong>Alternative: a self-contained zip for a manual upload</strong></summary>

Only if VS Code isn't available. This vendors **Linux** wheels into
`.python_packages/lib/site-packages/`, so the zip needs no build on the far side:

```powershell
python tools/poc/package_function.py
```

`dist/pyre-poc.zip` (~9 MB). Upload it via **Advanced Tools (Kudu) → Tools → Zip
Push Deploy**, then Restart the app. You'll see a pip warning about dependency
conflicts in your *local* environment — ignore it, the download is isolated
(`--target`) and doesn't touch your packages.

</details>

### 4c. Confirm it landed

Portal → **Function App** → **Overview**, scroll to the **Functions** list.
Expect four: `detect`, `health`, `ingest`, `bundle_published`.

If the list is empty, the app failed to import — open **Log stream** and see
[troubleshooting.md](troubleshooting.md#the-functions-list-is-empty-after-upload).

---

## Step 5 — bundle and upload your detections

This is the DaC half — the part that makes it a detection *platform*. Your
detections live in **your own repo**, and this step turns that repo into two
blobs.

### Exactly what the detections container must contain

Get this right and everything else follows. The container holds **two things**:

```
detections/                                  <- the blob container
├── current.json                             <- the POINTER, at the container root
└── bundles/
    └── sha256-1c5ffec782e77d97.zip          <- the BUNDLE
```

**`current.json`** — one small JSON object naming the bundle that's live:

```json
{"version": "sha256-1c5ffec782e77d97", "path": "bundles/sha256-1c5ffec782e77d97.zip"}
```

- `path` is relative to the **container root** and must match where the zip
  actually is, `bundles/` prefix included.
- `version` is any string. Workers reload **when and only when this value
  changes**, so it must be different every time the detections change. A content
  hash gives you that for free.

**The zip** — your detection files. Inside it:

```
detections/azure/eventhub_auth_failure.yml     <- metadata
detections/azure/eventhub_auth_failure.py      <- the rule() logic, SAME folder
detections/identity/signin_anomaly.yml
detections/identity/signin_anomaly.py
global_helpers/eh_helpers.yml
global_helpers/eh_helpers.py
```

The rules that actually matter:

| Rule | Why |
|---|---|
| **The folder layout inside the zip is entirely up to you.** The engine walks the whole tree recursively. | Nested folders, a wrapping top-level folder, everything flat — all fine. |
| **A detection is a `.yml` + a `.py`, and the `.py` must be in the SAME folder as its `.yml`.** | `Filename:` is resolved next to the YAML, using only its basename. This is the #1 thing that silently breaks a bundle. |
| **The `.yml` must declare `RuleID`, `Filename` and `LogTypes`.** | Missing any one and the engine skips it without an error. |
| **`LogTypes:` values must exactly equal what your records carry** in the field named by `LOG_TYPE_FIELD` (Step 2). | Case-sensitive. `RuntimeAuditLogs` ≠ `runtimeauditlogs`. |
| **Shared helpers need a `.yml` with `AnalysisType: global` + `Filename`.** | That's what puts their folder on the import path so `from eh_helpers import ...` resolves anywhere in the bundle. |
| **Don't ship `*_tests.py` or `__pycache__`.** | Test files import frameworks the engine doesn't have; bytecode is built for the wrong platform. |

A minimal valid `.yml`:

```yaml
AnalysisType: rule
Filename: eventhub_auth_failure.py     # must sit next to this file
RuleID: "Azure.EventHub.AuthFailure"
Enabled: true
Severity: Medium
LogTypes:
  - RuntimeAuditLogs                   # must match your data exactly
Threshold: 3                           # optional; 1 = alert on first match
DedupPeriodMinutes: 60
```

And its `.py` — only `rule()` is required:

```python
def rule(event):
    return event.get("ActivityStatus") == "Failure"

def title(event):                      # optional
    return f"Auth failure from {event.get('ClientIp')}"

def dedup(event):                      # optional; groups matches into one alert
    return event.get("ClientIp", "unknown")
```

A complete worked pair, with every optional hook, is in
[tools/poc/dac_bundler/example/](../../tools/poc/dac_bundler/example/).

### 5a. Put the bundler in your DaC repo

Copy the whole [tools/poc/dac_bundler/](../../tools/poc/dac_bundler/) folder into
your detections repo. It's self-contained — plain Python, no dependency on this
repo, no Azure anything. Then:

```bash
cd <your-dac-repo>
python dac_bundler/bundle.py
```

It excludes itself, `.git`, `__pycache__` and `*_tests.py` automatically. If your
rules live in a subfolder with helpers alongside:

```bash
python dac_bundler/bundle.py --source rules --extra global_helpers
```

**It validates before it zips**, and refuses to produce a bundle that would load
nothing — a `.py` in the wrong folder, a missing `RuleID`, unparseable YAML. Then
it prints the thing you need most:

```
validated 6 detection(s), 1 global helper(s)

LogTypes declared by these detections - an event's log-type field
must hold one of these EXACTLY, or it will never be routed:
      6  RuntimeAuditLogs
```

**Compare that list against the values you wrote down in Step 2.** If they don't
line up, nothing will ever fire, and this is where you find out — before
uploading, not after.

Output lands in `dist/`:

```
dist/current.json
dist/bundles/sha256-1c5ffec782e77d97.zip
```

The version is a hash of your detection files, so it changes automatically
whenever a rule changes — which is exactly what makes a running worker reload.

Full options: [dac_bundler/README.md](../../tools/poc/dac_bundler/README.md).

### 5b. Upload the two files

Portal → **Storage account** → **Storage browser** → **Blob containers** →
**detections**.

**Order matters — the zip first, the pointer second:**

1. **Upload** → pick `dist/bundles/<version>.zip` → expand **Advanced** → set
   **Upload to folder** to `bundles` → Upload.
2. **Upload** → pick `dist/current.json` → leave the folder blank (it goes at the
   container root) → tick **Overwrite if files already exist** → Upload.

That order is the whole reason publishing is two files: a worker must never be
able to read a pointer to a bundle that isn't there yet.

Old bundle zips are harmless — nothing reads them once the pointer moves on. Keep
a few; they're a one-click rollback (re-upload a `current.json` naming an older
one).

<details>
<summary><strong>Optional: a known-good bundle to test the plumbing first</strong></summary>

If you'd rather prove the upload path works before introducing your own
detections, this repo ships a small self-contained bundle:

```powershell
python tools/poc/publish_bundle.py
```

It writes the same two files to `dist/detections/`. Its rules are for
`AWS.CloudTrail` and won't match your Event Hub logs — the point is only to
confirm that a bundle loads and `/health` reports it. Swap in your own
immediately after.

</details>

---

## Step 6 — prove it works

### 6a. Is the engine loaded?

Portal → **Function App** → **Overview → Functions** → click **health** → **Get
function URL** → copy. Paste it in a browser tab.

```json
{
  "env": "poc",
  "state_backend": "memory",
  "log_type_field": "Category",
  "event_time_field": "Timestamp",
  "event_envelope_field": "records",
  "bundle_mode": "blob",
  "output_container": "pyre-output",
  "default_routes": [],
  "bundle_version": "sha256-1c5ffec782e77d97",
  "detections": 6,
  "log_types": ["RuntimeAuditLogs"],
  "status": "ok"
}
```

Read it carefully — it answers the two questions behind every "why no alerts?"
moment:

- **`detections`** — did your bundle actually load? `0` means the upload landed
  but nothing in it was a usable detection.
- **`log_types`** — this list is built from your detections' `LogTypes:`. It must
  contain the exact values you saw in Step 2. If `/health` says
  `["RuntimeAuditLogs"]` and your records carry `"Category": "OperationalLogs"`,
  nothing will ever fire, and this is the line that tells you.

Also confirm `log_type_field`, `event_time_field` and `event_envelope_field`
match what you found in Step 2.

A `503` with `"status": "bundle-load-failed"` means Step 5 didn't land; the
`error` field says why.

### 6b. Send logs straight to the detections

`ingest` runs the identical code path as the Event Hub trigger from
`process_batch` onward — it just skips Event Hubs. Use it to test the *detection*
half in isolation, so when something's wrong you know which half is at fault.

Copy a real message body out of Data Explorer (Step 2) into a file, envelope and
all — the endpoint accepts exactly what Event Hubs carries. Then, from
PowerShell:

```powershell
$url = "<paste the ingest function URL>"
Invoke-RestMethod -Method Post -Uri $url -ContentType "application/json" `
  -InFile my-sample.json
```

→ `accepted : 1`

That's one *message*. If it's an Azure envelope holding 12 records, the engine
evaluates 12 events from it.

This is an ordinary HTTPS POST — no Azure tooling involved. A file with several
messages, one JSON object per line, works too.

> No real data to hand yet?
> [tools/poc/samples/eventhub_diagnostic.jsonl](../../tools/poc/samples/eventhub_diagnostic.jsonl)
> is three Event Hubs `RuntimeAuditLogs` messages in the correct envelope shape —
> useful for checking the envelope unwraps before your own logs are flowing.

### 6c. Read the output

Portal → **Storage account** → **Storage browser** → **Blob containers** →
**pyre-output**.

Two folders, and you want to look at both:

| Blob | Contains | Read it to answer |
|---|---|---|
| `signals/<date>.jsonl` | one line per `rule()` that returned `True` | "did my detection match at all?" |
| `alerts/<date>.jsonl` | one line per alert | "did it survive threshold + dedup?" |

Click a file → the **Edit** tab shows the raw JSON lines.

Every record identifies itself, so the two streams stay readable even if you
concatenate them:

| Field | On | Meaning |
|---|---|---|
| `p_record_type` | both | `"signal"` or `"alert"` |
| `p_signal_id` | signals | unique per match |
| `p_alert_id` | alerts | unique per alert |
| `p_alert_id` | signals | **the alert this match rolled into**, or `null` if it never reached one |

That last row is the useful one. Filter the signals blob on
`p_alert_id == "<some id>"` and you get exactly the matches that made up that
alert; filter on `p_alert_id: null` and you get every match that was held back by
a threshold or by `CreateAlert: false`.

Signals are **never** deduplicated — they're the audit trail, and repeats are
real. Alerts are, so one alert appears once.

**Signals but no alerts** is not a failure — it's the engine's discipline
working. An alert requires the match to also clear the detection's `Threshold`
and not duplicate one already open within `DedupPeriodMinutes`. That gap is the
thing worth demonstrating.

**Neither?** Open **Function App → Monitoring → Log stream** and re-send. The
engine now tells you exactly what it saw:

```
no detections are registered for these log-type values: ApplicationMetricsLogs (1 event(s)).
A detection's YAML LogTypes must contain the value exactly.
```

```
3 event(s) in this batch had no value in the configured log-type field 'category'
- check LOG_TYPE_FIELD against your data
```

The first means routing worked but no detection covers that category. The second
means `LOG_TYPE_FIELD` is wrong — usually casing. Both name the real value, so
you can fix the setting or the `LogTypes:` and move on.

> Re-posting the exact same payload produces **nothing new**. That's the
> redelivery guard: with no transport event id, the processor hashes the body,
> and an identical body is treated as a duplicate. Change a field to send a
> genuinely new event.

---

## Step 7 — the real path: through the Event Hub

Everything above bypassed Event Hubs. Now do it properly — and entirely in the
portal.

Since your logs are Azure's own diagnostic records about Event Hub activity,
**the hub feeds itself**: connecting to it, sending, and failing to authorise all
generate `RuntimeAuditLogs` records that flow back in as events. So the honest
test is simply to generate some activity and wait.

**Generate activity** — Portal → **Event Hubs Namespace** → your hub → **Data
Explorer** → **Send events**, and send anything at all. That connection and send
are themselves audited.

Azure diagnostic logs are batched before delivery, so allow **several minutes**
(often up to 5) for records to appear. This is Azure's pipeline, not the engine.

Watch **Function App → Monitoring → Log stream** for the batch arriving, then
re-read the output blobs.

<details>
<summary>Prefer an instant, deterministic test?</summary>

Data Explorer → **Send events** with **Content type** `application/json`, pasting
one line of
[tools/poc/samples/eventhub_diagnostic.jsonl](../../tools/poc/samples/eventhub_diagnostic.jsonl)
as the body. That injects a correctly-shaped message immediately, without waiting
on Azure's diagnostic batching. It only matches if your bundle has a detection
for `RuntimeAuditLogs`.

</details>

> In-memory dedup state is still warm from Step 6b, so repeated identical
> activity may produce **no new alerts**. That is correct behaviour. For a clean
> run, **Restart** the app first (Overview → Restart).

---

## Changing a detection

This is the loop worth showing off, and it needs no redeploy:

1. Edit a rule in your DaC repo — say, lower a `Threshold:` so something that was
   below the bar now alerts.
2. `python dac_bundler/bundle.py`
3. Upload the new zip and the new `current.json` (Step 5b). The version string
   changed automatically, because it's a hash of the rule contents.
4. Wait 30 seconds. Re-check `/health` — `bundle_version` has changed.
5. Restart the app (to clear dedup state) and send the same logs again. The new
   threshold applies.

The Function App was never redeployed, and neither was anything else. That's the
DaC promise, demonstrated.

---

## Demo script

Roughly 5 minutes, in this order:

1. **Show a detection in your repo.** Open a `.py` and its `.yml` side by side.
   Point out that this is ordinary Python and ordinary metadata — the same format
   as panther-analysis, portable, reviewable in a pull request, versioned in git.
2. **Show it's loaded.** The `health` URL in a browser → `detections: N`,
   `bundle_version: ...`. Stress that this came from **Blob storage**, not from
   the deployment: the detections and the engine ship separately.
3. **Send logs.** Event Hubs Data Explorer (Step 7).
4. **Show the signals.** `pyre-output/signals/<today>.jsonl` — everything that
   matched, the complete audit trail.
5. **Show the alerts, and the gap.** `pyre-output/alerts/<today>.jsonl` — fewer
   lines than signals. Explain why: thresholds held back the noise, dedup
   collapsed repeats into one case. This is what makes it a detection platform
   rather than a grep loop, and it's the part people don't expect.
6. **Change a detection live.** The loop above. Edit the rule, bundle, upload two
   blobs, `bundle_version` changes, new behaviour. **No redeploy of anything.**

Step 6 of this list is the one that lands with an audience. Rehearse it, and have
the two blobs open in tabs before you start.

---

## What this POC does not prove

Say this out loud before anyone asks — it's the difference between a credible POC
and an oversold one.

| Not proven | Why | What it needs |
|---|---|---|
| **Correct state at scale** | Dedup/thresholds are per-worker and reset on cold start. One instance behaves right; two would double-count. | Redis (`STATE_BACKEND=redis`) — already written, just unwired |
| **Throughput / cost at volume** | Ten events on a low batch size says nothing about millions/hour | A load test, batch size 256+ |
| **Normalization** | The POC assumes logs already carry a log-type field. Real feeds don't. | Cribl (deliberately out of scope — see [architecture.md](../architecture.md)) |
| **Alert delivery** | Alerts go to a blob, not a case tool | Torq destination — the adapter already exists in `dispatch.py` |
| **Enrichment / lookup tables** | `p_enrichment` is stubbed | `enrichment.py` |
| **Network isolation** | Everything is on public endpoints | The VNet + private endpoints in `infra/` |
| **Automated DaC publishing** | You're uploading two blobs by hand | A pipeline running the same `bundle.py` — see [to-production.md](to-production.md#5-the-dac-publishes-itself) |
| **Scheduled / correlation detections** | Streaming rules only | A separate module |

The code paths for rows 1, 4 and 5 are already written and tested — the POC just
doesn't have the resources to switch them on, and switching them on is an app
setting each. That's a genuinely useful thing to be able to say, and
[to-production.md](to-production.md) is the receipt.

---

## App settings reference

| Setting | POC value | What it does |
|---|---|---|
| `PYRE_ENV` | `poc` | Environment label |
| `STATE_BACKEND` | `memory` | `memory` = in-process state; `redis` = production |
| `LOG_TYPE_FIELD` | e.g. `Category` | **The field the engine reads off each record to route to detections.** Must match your events exactly. |
| `EVENT_TIME_FIELD` | e.g. `Timestamp` | Field carrying the record's own timestamp |
| `EVENT_ENVELOPE_FIELD` | `records` | Field holding an array of records when one message carries many — Azure diagnostic logs always do. Empty = one message is one event. |
| `BUNDLE_MODE` | `blob` | Where detections come from (`blob` or `local`) |
| `BUNDLE_BLOB_ACCOUNT_URL` | `https://<storage>.blob.core.windows.net` | Account holding the `detections` container |
| `REFRESH_INTERVAL_SECONDS` | `30` | How often a warm worker re-checks the bundle pointer |
| `OUTPUT_BLOB_ACCOUNT_URL` | `https://<storage>.blob.core.windows.net` | Enables the append-blob sink. Unset = production path. |
| `OUTPUT_BLOB_CONTAINER` | `pyre-output` | Container for `alerts/` and `signals/` |
| `DEFAULT_ROUTES` | *(leave empty)* | Where alerts go when a detection doesn't specify |
| `AzureFunctionsJobHost__extensions__eventHubs__maxEventBatchSize` | `50` | Events per invocation (ceiling, not a wait) |
| `EVENTHUB_NAME` | *(already set)* | Resolved into the trigger's `%EVENTHUB_NAME%` |
| `EVENTHUB_CONNECTION` *or* `EVENTHUB_CONNECTION__*` | *(already set)* | Event Hub auth — see [troubleshooting.md](troubleshooting.md#the-event-hub-trigger-never-fires) |
| `SIGNALS_SINK_URL` | *(leave unset)* | Cribl endpoint. Unset → signals go to the blob instead. |
| `STORM_LIMIT` | *(default 1000)* | Max alerts per detection per hour |
| `AZURE_CLIENT_ID` | *(only for user-assigned MI)* | Selects which identity to use |

### The four functions

| Function | Trigger | Purpose |
|---|---|---|
| `detect` | Event Hub (batch) | **The one that matters.** Routes, evaluates, alerts. |
| `health` | HTTP GET | Which bundle is loaded, how many detections, which log types |
| `ingest` | HTTP POST | Feed logs directly, bypassing Event Hubs — isolates the detection half when debugging |
| `bundle_published` | Event Grid | Marks the bundle stale so a publish goes live immediately (optional — see [troubleshooting.md](troubleshooting.md#optional-event-grid-instant-reload)) |
