# pyre POC — complete setup guide

The goal of this POC is narrow and worth stating precisely:

> **Prove that a log arriving on an Event Hub is routed to the right
> Detections-as-Code rules, evaluated by `rule()`, and that a `True` produces an
> alert — using the real `.py` + `.yml` DaC files, in the company's Azure.**

Everything that isn't needed for that sentence has been left out: no Redis, no
Cribl, no Torq, no VNet, no Key Vault, no external communication of any kind.
Alerts land in a blob you can open and read.

**Time to a working demo: about 45 minutes**, most of it waiting on deployments.

---

## Contents

1. [What you're building](#1-what-youre-building)
2. [What changed in the code, and why](#2-what-changed-in-the-code-and-why)
3. [Before you start](#3-before-you-start)
4. [Step 1 — collect your resource names](#step-1--collect-your-resource-names)
5. [Step 2 — create the two blob containers](#step-2--create-the-two-blob-containers)
6. [Step 3 — give the Function App permission](#step-3--give-the-function-app-permission)
7. [Step 4 — app settings](#step-4--app-settings)
8. [Step 5 — deploy the function code](#step-5--deploy-the-function-code)
9. [Step 6 — publish the detections](#step-6--publish-the-detections)
10. [Step 7 — prove it works](#step-7--prove-it-works)
11. [Step 8 — the real path: through the Event Hub](#step-8--the-real-path-through-the-event-hub)
12. [Optional — Event Grid for instant detection reload](#optional--event-grid-for-instant-detection-reload)
13. [Optional — a pipeline that publishes the DaC](#optional--a-pipeline-that-publishes-the-dac)
14. [Demo script](#14-demo-script)
15. [What this POC does not prove](#15-what-this-poc-does-not-prove)
16. [App settings reference](#16-app-settings-reference)

Stuck? → **[troubleshooting.md](troubleshooting.md)**

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
             DaC published by │
             pipeline or hand │
```

**Signals vs alerts** — the distinction the demo hinges on. A *signal* is written
every time `rule()` returns `True`: a complete audit of everything that matched.
An *alert* is only raised when that match also clears the detection's `Threshold`
and isn't a duplicate of one already open. That is why 8 matches produce 3
alerts below.

---

## 2. What changed in the code, and why

Two production dependencies don't exist in the POC, so each got a drop-in
substitute behind the interface that was already there. **The detection path
itself — routing, `rule()`, signals, thresholds, dedup — is completely
unmodified**, which is the point: what you demo is the real engine.

| Missing | Substitute | Where |
|---|---|---|
| **Redis** (dedup / thresholds / `unique()` / storm limit / redelivery guard) | An in-process store implementing the handful of Redis commands the engine uses, injected through `StateStore`'s existing `client=` seam. `dedup.py` and `processor.py` are untouched. | [engine/pyre_engine/state.py](../../engine/pyre_engine/state.py) |
| **Cribl lake + Torq** (where signals and alerts go) | An **append blob** — one JSON object per line, appended to the end, readable in the portal. Append is a single server-side operation, so concurrent workers can't clobber each other. | [engine/pyre_engine/blobsink.py](../../engine/pyre_engine/blobsink.py) |

Both are selected by app settings (`STATE_BACKEND`, `OUTPUT_BLOB_ACCOUNT_URL`);
leave them unset and the code takes the production path unchanged. That is the
honest trade of the in-memory store, stated plainly:

> Dedup windows, thresholds and the redelivery guard live **inside one worker
> process** and reset on a cold start. With one instance and a short demo that
> behaves identically to Redis. It is not correct across scale-out, and it is
> not production. Flip `STATE_BACKEND=redis` when the resource exists — nothing
> else changes.

The Function App also gained three small functions beside `detect`, all of them
there to make the POC demonstrable and debuggable — `health`, `ingest`,
`bundle_published`. See [§16](#16-app-settings-reference) and
[engine/function_app.py](../../engine/function_app.py).

---

## 3. Before you start

```bash
az --version                      # Azure CLI
func --version                    # Azure Functions Core Tools v4
python --version                  # 3.11 or 3.12
az login
az account set --subscription "<your subscription>"
```

Install the Python bits you'll run locally:

```bash
pip install -r engine/requirements.txt
```

You need to be **Owner or User Access Administrator** on the resource group for
[Step 3](#step-3--give-the-function-app-permission) (it assigns roles). If you
aren't, hand your architect the three `az role assignment create` commands.

---

## Step 1 — collect your resource names

Fill these in once; every command below uses them.

```bash
RG=<resource-group>
APP=pyre                                   # the Function App
STORAGE=<storage-account-name>             # the one already attached to the app
EHNS=<eventhub-namespace>                  # without .servicebus.windows.net
HUB=<event-hub-name>                       # the hub inside that namespace
```

PowerShell:

```powershell
$RG="<resource-group>"; $APP="pyre"; $STORAGE="<storage-account-name>"
$EHNS="<eventhub-namespace>"; $HUB="<event-hub-name>"
```

Confirm they're right — this catches most later failures:

```bash
az functionapp show -g $RG -n $APP --query "{name:name, state:state, kind:kind, sku:sku}" -o json
az eventhubs eventhub list -g $RG --namespace-name $EHNS --query "[].name" -o tsv
az storage account show -g $RG -n $STORAGE --query name -o tsv
```

---

## Step 2 — create the two blob containers

The storage account has the four containers Functions made for itself. Add two:

| Container | Holds |
|---|---|
| `detections` | the published DaC bundle + the `current.json` version pointer |
| `pyre-output` | `alerts/<date>.jsonl` and `signals/<date>.jsonl` — the demo artifact |

```bash
az storage container create --account-name $STORAGE --name detections  --auth-mode login
az storage container create --account-name $STORAGE --name pyre-output --auth-mode login
```

> The engine creates `pyre-output` itself if it's missing, so this is belt and
> braces. `detections` must exist before you publish.

---

## Step 3 — give the Function App permission

The app authenticates to storage (and optionally Event Hubs) with its **managed
identity** — no keys, no connection strings in code.

```bash
# Turn on the system-assigned identity and capture its principal id.
PRINCIPAL=$(az functionapp identity assign -g $RG -n $APP --query principalId -o tsv)
STORAGE_ID=$(az storage account show -g $RG -n $STORAGE --query id -o tsv)

# Read the detections bundle, write the alert/signal blobs.
az role assignment create --assignee $PRINCIPAL \
  --role "Storage Blob Data Contributor" --scope $STORAGE_ID
```

PowerShell:

```powershell
$PRINCIPAL = az functionapp identity assign -g $RG -n $APP --query principalId -o tsv
$STORAGE_ID = az storage account show -g $RG -n $STORAGE --query id -o tsv
az role assignment create --assignee $PRINCIPAL --role "Storage Blob Data Contributor" --scope $STORAGE_ID
```

You also need **Storage Blob Data Contributor on yourself**, so you can publish
the bundle and read the output from your laptop:

```bash
ME=$(az ad signed-in-user show --query id -o tsv)
az role assignment create --assignee $ME --role "Storage Blob Data Contributor" --scope $STORAGE_ID
```

> Role assignments take **up to 5 minutes** to take effect. A `403` right after
> this step usually just means "wait a bit".

If the app already uses a **user-assigned** identity instead, use its principal
id here and set `AZURE_CLIENT_ID` to its *client* id in Step 4.

---

## Step 4 — app settings

This is where you tell the engine which field to route on. **`LOG_TYPE_FIELD` is
the setting you asked about**: the engine reads that field off every event and
runs only the detections whose YAML `LogTypes:` lists its value.

For the POC bundle, events carry `"dataset": "AWS.CloudTrail"` and the rules
declare `LogTypes: [AWS.CloudTrail]`, so the default `dataset` is correct.

```bash
az functionapp config appsettings set -g $RG -n $APP --settings \
  PYRE_ENV=poc \
  STATE_BACKEND=memory \
  LOG_TYPE_FIELD=dataset \
  EVENT_TIME_FIELD=_time \
  EVENTHUB_NAME=$HUB \
  BUNDLE_MODE=blob \
  BUNDLE_BLOB_ACCOUNT_URL=https://$STORAGE.blob.core.windows.net \
  REFRESH_INTERVAL_SECONDS=30 \
  OUTPUT_BLOB_ACCOUNT_URL=https://$STORAGE.blob.core.windows.net \
  OUTPUT_BLOB_CONTAINER=pyre-output \
  DEFAULT_ROUTES=blob_alerts \
  SIGNALS_SINK_URL= \
  AzureFunctionsJobHost__extensions__eventHubs__maxEventBatchSize=50
```

Then the Event Hub trigger connection. **Pick one.**

**(a) Connection string — simplest, recommended for the POC.**

```bash
EH_CONN=$(az eventhubs namespace authorization-rule keys list \
  -g $RG --namespace-name $EHNS --name RootManageSharedAccessKey \
  --query primaryConnectionString -o tsv)

az functionapp config appsettings set -g $RG -n $APP \
  --settings "EVENTHUB_CONNECTION=$EH_CONN"
```

**(b) Managed identity — no secret, one extra role.** Same code, different
settings; this is what production uses.

```bash
az role assignment create --assignee $PRINCIPAL \
  --role "Azure Event Hubs Data Receiver" \
  --scope $(az eventhubs namespace show -g $RG -n $EHNS --query id -o tsv)

az functionapp config appsettings set -g $RG -n $APP --settings \
  EVENTHUB_CONNECTION__fullyQualifiedNamespace=$EHNS.servicebus.windows.net \
  EVENTHUB_CONNECTION__credential=managedidentity
```

> Set **either** `EVENTHUB_CONNECTION` **or** the two `__`-suffixed settings,
> never both — the runtime resolves the plain one first and you'll get confusing
> auth errors.

`maxEventBatchSize=50` is deliberately low so a handful of demo events arrive as
one visible batch. It is a **ceiling, not a wait**: small backlogs are still
delivered immediately, so this doesn't delay anything. Production runs 256+.

---

## Step 5 — deploy the function code

**Recommended: Core Tools.** It builds the Python dependencies *on the Linux
worker*, so you never have to produce Linux wheels from a Windows laptop.

```bash
cd engine
func azure functionapp publish pyre --python
cd ..
```

`engine/` is the app root: `function_app.py`, `host.json`, `requirements.txt` and
the `pyre_engine/` package sit at the top level of the deployed package. **No
config files are needed in the zip** — every setting the POC uses is an app
setting, which is why this step is one command.

<details>
<summary><strong>Alternative: build a zip and upload it by hand</strong> (you mentioned wanting this)</summary>

Worth doing only if Core Tools is unavailable or blocked. The script vendors
Linux wheels into `.python_packages/lib/site-packages/`, exactly where the worker
looks, so the zip needs no build on the far side:

```bash
python tools/poc/package_function.py
az functionapp deployment source config-zip -g $RG -n $APP --src dist/pyre-poc.zip
```

**Recommendation: use Core Tools.** Both produce the same result, but the manual
zip has two failure modes Core Tools doesn't — a dependency with no
`manylinux` wheel, and a stale zip you forgot to rebuild. Pointing the app at a
repo isn't worth it for a POC: it adds a deployment credential and a build
pipeline to debug, for a step that is already one command.

</details>

Confirm the four functions registered:

```bash
az functionapp function list -g $RG -n $APP --query "[].{name:name}" -o table
```

Expect `detect`, `health`, `ingest`, `bundle_published`.

---

## Step 6 — publish the detections

This is the DaC half. **Two paths — start with A, move to B when A is boring.**

### A. The curated POC bundle (do this first)

Three self-contained rules that are guaranteed to load and fire, so your first
run proves the *plumbing* rather than debugging someone else's detections.

```bash
python tools/poc/publish_bundle.py \
  --account-url https://$STORAGE.blob.core.windows.net
```

It zips `tools/poc/dac/`, uploads `bundles/<version>.zip`, then flips
`current.json`. Bundle first, pointer last — so a worker can never read a
pointer to a bundle that isn't there yet.

### B. The real external DaC repo

The actual "detections as code" story: pull from the DaC repo, publish the
result. `config/detections.yaml` points at panther-analysis today; point it at
your fork.

```bash
python cli/pyre pull                    # clone the DaC repo → .bundle/
python cli/pyre validate                # optional: lint before publishing
python tools/poc/publish_bundle.py --dir .bundle \
  --account-url https://$STORAGE.blob.core.windows.net
```

`pyre pull` stamps `.bundle/.bundle-version` with the DaC commit sha, so what's
running is always traceable to a commit.

> **Expect some rules to be skipped.** Of panther-analysis's full set, ~770
> detections across 91 log types load; the rest reference helper modules that
> weren't pulled. A detection that won't import is skipped and logged — it never
> blocks the rest of the bundle. `/health` tells you exactly how many loaded.

### The manual fallback you asked about

If neither script can reach storage, publishing is just two blobs:

```bash
# 1. zip the detections (the .py/.yml pairs at the ROOT of the zip)
cd tools/poc/dac && zip -r ../../../bundle.zip . && cd ../../..

# 2. upload the bundle, THEN the pointer — never the other way round
az storage blob upload --account-name $STORAGE -c detections \
  -n bundles/manual-001.zip -f bundle.zip --auth-mode login --overwrite

echo '{"version":"manual-001","path":"bundles/manual-001.zip"}' > current.json
az storage blob upload --account-name $STORAGE -c detections \
  -n current.json -f current.json --auth-mode login --overwrite
```

Bump `manual-001` every time. **The version string is what triggers the
reload** — workers compare it and only re-download when it changes.

You can also do this entirely in the portal: upload the zip, then upload a
`current.json` naming it.

---

## Step 7 — prove it works

### 7a. Is the engine loaded?

```bash
KEY=$(az functionapp function keys list -g $RG -n $APP --function-name health --query default -o tsv)
curl "https://$APP.azurewebsites.net/api/health?code=$KEY"
```

```json
{
  "env": "poc",
  "state_backend": "memory",
  "log_type_field": "dataset",
  "bundle_mode": "blob",
  "output_container": "pyre-output",
  "default_routes": ["blob_alerts"],
  "bundle_version": "sha256-5765a90e3c708bcd",
  "detections": 3,
  "log_types": ["AWS.CloudTrail"],
  "status": "ok"
}
```

Read it carefully — it answers the two questions that cause every "why no
alerts?" moment:

- **`detections`** — did the bundle actually load?
- **`log_types`** — this list must contain the exact value your events carry in
  `log_type_field`. `AWS.CloudTrail` ≠ `aws.cloudtrail`.

A `503` with `"status": "bundle-load-failed"` means Step 6 didn't land; the
`error` field says why.

### 7b. Send logs straight to the detections

`ingest` runs the identical code path as the Event Hub trigger from
`process_batch` onward — it just skips Event Hubs. Use it to test the *detection*
half in isolation, so if something's wrong you know which half.

```bash
KEY=$(az functionapp function keys list -g $RG -n $APP --function-name ingest --query default -o tsv)
curl -X POST "https://$APP.azurewebsites.net/api/ingest?code=$KEY" \
  -H "Content-Type: application/json" \
  --data-binary @tools/poc/samples/cloudtrail_poc.jsonl
```

PowerShell:

```powershell
$KEY = az functionapp function keys list -g $RG -n $APP --function-name ingest --query default -o tsv
Invoke-RestMethod -Method Post -Uri "https://$APP.azurewebsites.net/api/ingest?code=$KEY" `
  -ContentType "application/json" `
  -InFile tools/poc/samples/cloudtrail_poc.jsonl
```

→ `{"accepted": 10}`

### 7c. Read the alerts

```bash
python tools/poc/read_output.py --account-url https://$STORAGE.blob.core.windows.net
```

```
pyre-output/alerts/2026-07-30.jsonl: 3 record(s)

[High    ] AWS root console login from 203.0.113.10 in account [123456789012]
           detection=POC.AWS.Console.RootLogin  dedup=root-login:123456789012

[Medium  ] IAM user [backdoor-svc] created by root in account [123456789012]
           detection=POC.AWS.IAM.UserCreated  dedup=iam-user-created:123456789012:backdoor-svc

[Medium  ] Repeated failed AWS console logins for alice in account [123456789012]
           detection=POC.AWS.Console.LoginFailed  dedup=login-failure:123456789012:alice
```

And the full audit trail:

```bash
python tools/poc/read_output.py --account-url https://$STORAGE.blob.core.windows.net --stream signals
```

**10 events in → 8 signals → 3 alerts.** That gap is the whole story:

| Events | Rule | Outcome |
|---|---|---|
| 2 root logins (different IPs) | `RootLogin` | 2 signals → **1 alert** — same dedup string, grouped |
| 1 `CreateUser` | `UserCreated` | 1 signal → **1 alert** |
| 4 failed logins, user `alice` | `LoginFailed` | 4 signals → **1 alert** — `Threshold: 3` cleared, then deduped |
| 1 failed login, user `bob` | `LoginFailed` | 1 signal → **no alert** — below threshold |
| 2 unrelated API calls | — | nothing at all |

You can also just open `pyre-output` in the portal and read
`alerts/<today>.jsonl` — it's plain JSON lines.

> Re-running the exact same `ingest` payload produces **nothing new**. That's the
> redelivery guard working: with no transport event id, the processor hashes the
> body, and an identical body is treated as a duplicate. Change a field to send a
> genuinely new event.

---

## Step 8 — the real path: through the Event Hub

Everything above bypassed Event Hubs. Now do it properly.

```bash
python tools/testlab/python_shipper.py \
  --namespace $EHNS.servicebus.windows.net \
  --hub $HUB \
  --file tools/poc/samples/cloudtrail_poc.jsonl \
  --rate 10
```

This sends with your `az login` identity, so grant yourself sender rights once:

```bash
az role assignment create --assignee $ME \
  --role "Azure Event Hubs Data Sender" \
  --scope $(az eventhubs namespace show -g $RG -n $EHNS --query id -o tsv)
```

Wait ~30 seconds, then read the output again (§7c). Because these are the same
10 events you already ingested and the in-memory dedup state is still warm, you
may see no *new* alerts — that is correct behaviour, not a failure. To see fresh
alerts, either restart the app (`az functionapp restart -g $RG -n $APP`) to clear
in-memory state, or edit a field in the sample file first.

Watch it live:

```bash
func azure functionapp logstream pyre
```

---

## Optional — Event Grid for instant detection reload

You have an Event Grid resource, and there's a clean use for it.

By default a worker re-checks the bundle pointer every
`REFRESH_INTERVAL_SECONDS`, so a publish goes live within ~30s. Wiring Event Grid
turns that poll into a **push**: a blob write in `detections` fires the
`bundle_published` function, which marks the bundle stale so the very next batch
reloads it.

```bash
FUNC_ID=$(az functionapp show -g $RG -n $APP --query id -o tsv)/functions/bundle_published

az eventgrid system-topic create -g $RG -n pyre-storage-topic \
  --source $STORAGE_ID --topic-type Microsoft.Storage.StorageAccounts \
  --location $(az storage account show -g $RG -n $STORAGE --query location -o tsv)

az eventgrid system-topic event-subscription create -g $RG \
  --system-topic-name pyre-storage-topic --name pyre-bundle-published \
  --endpoint-type azurefunction --endpoint $FUNC_ID \
  --included-event-types Microsoft.Storage.BlobCreated \
  --subject-begins-with /blobServices/default/containers/detections/
```

The function only marks the registry stale; it never reloads inline. So a bad
publish can't take detection down — the worker keeps serving the last-good
registry. **Skip this if you're short on time**; the 30-second poll is fine for a
demo.

---

## Optional — a pipeline that publishes the DaC

Your backup plan (pull manually, upload manually) is Step 6's fallback and it
works fine. If you want the pipeline, the repo already has one:
[.azure-pipelines/publish-detections.yml](../../.azure-pipelines/publish-detections.yml).

For the POC it needs three things:

1. A service connection whose identity has **Storage Blob Data Contributor** on
   the storage account.
2. `BUNDLE_BLOB_ACCOUNT_URL` = `https://<storage>.blob.core.windows.net`.
3. A repository trigger on the DaC repo, so a push to a detection publishes it.

**Do this last.** Get the manual path working end to end first — then the
pipeline is just automating a sequence you've already proven.

---

## 14. Demo script

Roughly 5 minutes, in this order:

1. **Show a detection.** Open
   [aws_console_root_login.py](../../tools/poc/dac/aws_cloudtrail/aws_console_root_login.py)
   and its `.yml`. Point out that this is ordinary Python and ordinary metadata —
   the same format as panther-analysis, portable, reviewable in a pull request.
2. **Show it's loaded.** `curl .../api/health` → `detections: 3`,
   `bundle_version: ...`. This came from Blob storage, not from the deployment.
3. **Send logs.** Run the shipper (§8) into the Event Hub.
4. **Show the alerts.** `read_output.py` → three alerts, with titles and context
   generated by the detections' own `title()` and `alert_context()` functions.
5. **Show the discipline.** 8 matched, 3 alerted. Walk the table in §7c — dedup
   collapsed the root logins, the threshold held back `bob`. This is the part
   that makes it a detection *platform* rather than a grep loop.
6. **Change a detection live.** Edit a rule in `tools/poc/dac/`, re-run
   `publish_bundle.py`, wait 30 seconds, send the logs again. **No redeploy.**
   That's the DaC promise, demonstrated.

Step 6 is the one that lands with an audience. Rehearse it.

---

## 15. What this POC does not prove

Say this out loud before anyone asks — it's the difference between a credible POC
and an oversold one.

| Not proven | Why | What it needs |
|---|---|---|
| **Correct state at scale** | Dedup/thresholds are per-worker and reset on cold start. One instance behaves right; two would double-count. | Redis (`STATE_BACKEND=redis`) — already written, just unwired |
| **Throughput / cost at volume** | Ten events on a low batch size says nothing about millions/hour | Load test with `python_shipper.py --rate --loop`, batch size 256+ |
| **Normalization** | The POC assumes logs already carry a log-type field. Real feeds don't. | Cribl (this is deliberately out of scope — see [architecture.md](../architecture.md)) |
| **Alert delivery** | Alerts go to a blob, not a case tool | Torq destination — the adapter already exists in `dispatch.py` |
| **Enrichment / lookup tables** | `p_enrichment` is stubbed | `enrichment.py` |
| **Network isolation** | Everything is on public endpoints | The VNet + private endpoints in `infra/` |
| **Scheduled / correlation detections** | Streaming rules only | A separate module |

The engine code paths for the first, fourth and sixth rows are already written
and tested — the POC just doesn't have the resources to switch them on. That's a
genuinely useful thing to be able to say.

---

## 16. App settings reference

| Setting | POC value | What it does |
|---|---|---|
| `PYRE_ENV` | `poc` | Environment label |
| `STATE_BACKEND` | `memory` | `memory` = in-process state; `redis` = production |
| `LOG_TYPE_FIELD` | `dataset` | **The field the engine reads to route to detections.** Must match your events. |
| `EVENT_TIME_FIELD` | `_time` | Field carrying the event's own timestamp |
| `EVENTHUB_NAME` | your hub | Resolved into the trigger's `%EVENTHUB_NAME%` |
| `EVENTHUB_CONNECTION` | connection string | Event Hub auth — *or* the two `__` settings below |
| `EVENTHUB_CONNECTION__fullyQualifiedNamespace` | `<ns>.servicebus.windows.net` | Managed-identity alternative |
| `EVENTHUB_CONNECTION__credential` | `managedidentity` | Managed-identity alternative |
| `BUNDLE_MODE` | `blob` | Where detections come from (`blob` or `local`) |
| `BUNDLE_BLOB_ACCOUNT_URL` | `https://<storage>.blob.core.windows.net` | Account holding the `detections` container |
| `REFRESH_INTERVAL_SECONDS` | `30` | How often a warm worker re-checks the bundle pointer |
| `OUTPUT_BLOB_ACCOUNT_URL` | `https://<storage>.blob.core.windows.net` | Enables the append-blob sink. Unset = production path. |
| `OUTPUT_BLOB_CONTAINER` | `pyre-output` | Container for `alerts/` and `signals/` |
| `DEFAULT_ROUTES` | `blob_alerts` | Where alerts go when a detection doesn't specify |
| `SIGNALS_SINK_URL` | *(empty)* | Cribl endpoint. Empty → signals go to the blob instead. |
| `STORM_LIMIT` | *(default 1000)* | Max alerts per detection per hour |
| `AzureFunctionsJobHost__extensions__eventHubs__maxEventBatchSize` | `50` | Events per invocation (ceiling, not a wait) |
| `AZURE_CLIENT_ID` | *(only for user-assigned MI)* | Selects which identity to use |

### The four functions

| Function | Trigger | Purpose |
|---|---|---|
| `detect` | Event Hub (batch) | **The one that matters.** Routes, evaluates, alerts. |
| `health` | HTTP GET | Which bundle is loaded, how many detections, which log types |
| `ingest` | HTTP POST | Feed logs directly, bypassing Event Hubs — isolates the detection half when debugging |
| `bundle_published` | Event Grid | Marks the bundle stale so a publish goes live immediately |
