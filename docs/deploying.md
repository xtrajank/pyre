# Deploying an instance

One pass through this and you have a running pyre: logs arriving on an Event Hub,
routed to detections, producing signals and alerts at the destination you chose.

**What an instance is for is entirely its App settings.** There is no mode, no
build flag and no branch that makes one deployment a trial and another the real
thing — the same package, pointed at different resources, with a different
destination. Stand up as many as you need.

---

## Before you start

You need: an Event Hubs namespace with a hub, a storage account, and an empty
Function App (**Linux, Python 3.11**). On your laptop: Python 3.11+, VS Code with
the **Azure Functions** extension, and this repo.

**Prove the engine works before touching Azure.** Two seconds, no cloud:

```powershell
pip install -r requirements.txt
python tools/run_local.py
```

You want **4 signals and 1 alert**. If you see that, every problem from here on
is an Azure configuration problem, not a code problem. That is worth knowing up
front.

Keep four Function App blades to hand: **Overview** (URL, Restart), **Settings →
Environment variables**, **Overview → Functions**, **Monitoring → Log stream**.

---

## 1. Storage containers

Storage account → **Data storage → Containers** → **+ Container**, leaving access
level **Private**:

| Container | Holds | Setting |
|---|---|---|
| `detections` | the published bundle + `current.json` | `DETECTIONS_CONTAINER` |
| `pyre-output` | `signals/<date>.jsonl`, `alerts/<date>.jsonl` | `SIGNAL_BLOB_CONTAINER` / `ALERT_BLOB_CONTAINER` |

Skip the second if this instance writes to HTTP destinations instead — see
[configuring-destinations.md](configuring-destinations.md).

Containers already there (`azure-webjobs-hosts`, `azure-webjobs-secrets`,
`app-package`, `$logs`) belong to the Functions runtime. Leave them alone. One
more, `azure-webjobs-eventhub`, appears on its own once an Event Hub trigger
starts listening — leave that alone too, but remember where it is: it is the
proof in [step 7](#7-prove-the-real-path).

## 2. Identity and roles

The app reads detections and writes output **as itself**, with no keys and no
connection strings.

1. Function App → **Settings → Identity** → **System assigned** → **On** → Save.
2. Storage account → **Access Control (IAM)** → **+ Add → Add role assignment** →
   **Storage Blob Data Contributor** → **Managed identity** → your Function App.
3. Event Hubs namespace → **Access Control (IAM)** → **Azure Event Hubs Data
   Receiver** → same identity.
4. Only if this instance uses `STATE_BACKEND=redis`: Azure Cache for Redis →
   **Data Access configuration** → **Redis Cache Contributor** → same identity.

> Role assignments take **up to 5 minutes** to apply. A 403 in the first few
> minutes is usually just this.

### If your host storage is identity-based

Check **Settings → Environment variables** for `AzureWebJobsStorage`:

| What you see | What it means |
|---|---|
| `DefaultEndpointsProtocol=...` | A connection string. Step 2 above is all you need. |
| `AzureWebJobsStorage__blobServiceUri` + `__credential` | The host reaches its own storage **as the managed identity**. Better, and it needs more roles. |

For the second, assign these instead of Blob Data Contributor alone:

| Role | Why |
|---|---|
| **Storage Blob Data Owner** | Microsoft's documented minimum for identity-based `AzureWebJobsStorage`, and separately the minimum the **Event Hub trigger** needs for its checkpoints. Covers step 1's containers too. |
| **Storage Queue Data Contributor** | matches `__queueServiceUri` |
| **Storage Table Data Contributor** | matches `__tableServiceUri`; also where Functions writes the diagnostic events that explain a host that won't start |

A **user-assigned** identity additionally needs `AzureWebJobsStorage__clientId`
set to its Client ID — the host resolves each connection's identity separately,
and this one is not covered by `AZURE_CLIENT_ID`. See
[configuration § Identity](configuration.md#identity).

## 3. Find out what your logs actually look like

**Do not skip this.** Two properties of your data decide two settings, and
getting either wrong produces the same symptom: everything appears to work and no
alerts ever appear.

Event Hubs Namespace → your hub → **Data Explorer → View events** → click an
event → **Body**.

**Are records wrapped in an envelope?** Azure diagnostic settings batch many
records into one message:

```json
{ "records": [ { "category": "...", "time": "..." }, { ... } ] }
```

That is `envelope_field: records`. One message = one record is `envelope_field: ""`.

**Which field routes, and what values does it hold?** For Azure diagnostic logs
it is `category`. Write down its exact value — that string has to appear
character for character in a detection's `LogTypes:`. Casing varies by resource
(`category` vs `Category`, `time` vs `Timestamp`).

## 4. Point the repo at your hub

```powershell
cp config/sources.example.yaml config/sources.yaml
```

Edit it with your namespace label and hub, plus whatever step 3 told you:

```yaml
namespaces:
  - namespace: platform
    hubs:
      - hub: logs-in
        log_type_field: category
        event_time_field: time
        envelope_field: records
```

`namespace:` is a short label **you choose**, not a resource name. It becomes
both the app setting the trigger authenticates with (`EVENTHUB_PLATFORM`) and
part of the Azure function name. Full runbook, including a second namespace:
[adding-a-log-source.md](adding-a-log-source.md).

> ### `config/sources.yaml` is gitignored
>
> It describes your infrastructure, so it is not committed on a public branch —
> only `config/sources.example.yaml` is. **It still ships inside the deployment**:
> a VS Code deploy sends your working tree, so this just works. See
> [log sources in a pipeline](#log-sources-in-a-pipeline) for the CI case.

## 5. App settings

Function App → **Settings → Environment variables → App settings**. Every setting
and its meaning: [configuration.md](configuration.md).

```
FUNCTIONS_WORKER_RUNTIME  = python
INSTANCE_LABEL            = <anything; echoed by /health>

EVENTHUB_PLATFORM__fullyQualifiedNamespace = <namespace>.servicebus.windows.net
EVENTHUB_PLATFORM__credential              = managedidentity

DETECTIONS_SOURCE            = blob
DETECTIONS_BLOB_ACCOUNT_URL  = https://<account>.blob.core.windows.net
DETECTIONS_CONTAINER         = detections
DETECTIONS_REFRESH_SECONDS   = 60

SIGNAL_DESTINATION       = blob
SIGNAL_BLOB_ACCOUNT_URL  = https://<account>.blob.core.windows.net
ALERT_DESTINATION        = blob
ALERT_BLOB_ACCOUNT_URL   = https://<account>.blob.core.windows.net

STATE_BACKEND               = memory
ALERT_STORM_LIMIT_PER_HOUR  = 1000
```

`EVENTHUB_<NAMESPACE>` is **derived from the `namespace:` label**, not typed by a
source — `/health` echoes the exact name each trigger wants under
`sources[].connection`, so there is nothing to work out by hand.

**What changes between instances**, and nothing else does:

| Setting | An instance writing where you can read it | An instance feeding an external SIEM |
|---|---|---|
| `SIGNAL_DESTINATION` | `blob` | `http` + `SIGNAL_HTTP_URL` |
| `ALERT_DESTINATION` | `blob` | `http` + `ALERT_HTTP_URL` + `ALERT_HTTP_AUTH_HEADER` |
| `STATE_BACKEND` | `memory` | `redis` + `REDIS_HOST` |
| `DETECTIONS_BLOB_ACCOUNT_URL` | that instance's account | that instance's account |
| `config/sources.yaml` | one hub | every hub |

[configuring-destinations.md](configuring-destinations.md) has worked settings
for both.

## 6. Deploy

VS Code → Azure extension → **Function App → Deploy to Function App** → pick the
app. The repo root **is** the Function App root; `.funcignore` excludes
everything that isn't runtime.

Or from Azure DevOps with [`azure-pipelines.yml`](../azure-pipelines.yml) — see
[below](#deploying-from-a-pipeline).

Confirm it landed: **Overview → Functions** should list `health`, `ingest` and one
`detect_<namespace>_<hub>` per source. **An empty list means `function_app.py`
raised while importing** — see
[troubleshooting](troubleshooting.md#the-functions-list-is-empty-after-a-successful-deploy).

## 7. Publish detections, then prove the path

**Publish** (from your detections repo — [`dac/`](../dac/) is the starter):

```bash
python publish.py --upload https://<account>.blob.core.windows.net
```

**Is the engine loaded?** `GET /api/health?code=<function key>`:

```json
{ "status": "ok", "problems": [], "detections": 1,
  "log_types": ["RuntimeAuditLogs"],
  "destinations": { "signal": "blob https://acct.blob.core.windows.net/pyre-output",
                    "alert":  "blob https://acct.blob.core.windows.net/pyre-output" } }
```

`problems` empty and `detections` above zero is the whole check. Anything else is
named there in words.

**Does the detection half work?** `POST /api/ingest?code=<key>` with a message
copied straight out of Data Explorer, envelope and all. It runs the identical
path from `process_batch` onward, so a 202 plus records at your destination
isolates the engine from the transport.

**Does the real path work?** Send something through the hub and read **Monitoring
→ Log stream**. One line per invocation:

```
batch platform/logs-in msgs=3 events=7 new=7 signals=4 alerts=1 12ms
```

That line is the answer to "is it running?". [operations.md](operations.md)
explains every field and what the warnings under it mean.

**Is the trigger actually listening?** Neither the function list nor `/health`
can tell you — the listener lives in the Functions host, not in the Python
worker. Storage account → **Containers → `azure-webjobs-eventhub`**: `ownership/`
blobs with a ticking **Last modified** are proof. Full checklist:
[troubleshooting § Is the trigger actually listening?](troubleshooting.md#is-the-trigger-actually-listening).

---

## Deploying from a pipeline

[`azure-pipelines.yml`](../azure-pipelines.yml) runs the tests, then one deploy
stage per instance. Each stage is a `- template:` block naming its own service
connection and Function App:

```yaml
- template: azure-pipelines-deploy.yml
  parameters:
    instance: production
    azureSubscription: <production-service-connection>
    appName: <production-function-app>
    environment: production     # attach an approval check to this
    dependsOn: [deploy_staging]
```

**Adding an instance is one more block.** Each gets its own ARM service
connection — sharing one means a stage can deploy over another instance's app.
Omit `environment:` to deploy without a gate.

Setup: push to Azure Repos → **Pipelines → New → Existing YAML file** → **Project
settings → Service connections** → one Azure Resource Manager connection per
instance → **Pipelines → Environments** for anything you want gated.

The build agent's outbound IPs are not your laptop's, so if the Function App
restricts inbound access you either allow the agent range or use a self-hosted
agent. That, and "our IP changed", is the argument for pipelines over laptops.

### Log sources in a pipeline

`config/sources.yaml` is gitignored, so a pipeline building from a clean clone
does not have one — and an app deployed without it registers **zero triggers**
while still looking healthy in the function list.

The pipeline fails on this deliberately, before deploying:

```
##vso[task.logissue type=error]config/sources.yaml is missing.
The app would deploy with no Event Hub triggers at all.
```

Two ways to satisfy it:

- **Commit it on your internal branch.** Drop the `config/sources.yaml` line from
  [`.gitignore`](../.gitignore). It contains no secrets by design — only labels
  you chose and hub names — so this is the normal answer once the repo is
  internal.
- **Generate it before the deploy step**, from a variable group or a secure file,
  if the hub list is sensitive in your environment.

---

## Sizing and scale

| Plan | What's needed |
|---|---|
| **Consumption / Flex Consumption** | Nothing. The scale controller wakes the app for Event Hub traffic. |
| **Premium (EP*)** | Nothing normally. With **VNet integration or private endpoints**, turn on **Configuration → Function runtime settings → Runtime scale monitoring**, or the scale controller can't see the hub and won't scale off zero. |
| **App Service (Dedicated)** | **Always On** must be **On**, or the host idles out and the listener dies with it — the classic "worked for an hour, then stopped". |

**Partition count is the parallelism ceiling.** One consumer group can have at
most one active reader per partition, so a 4-partition hub will never use more
than 4 concurrent workers however far the plan scales. Size partitions when you
create the hub; they cannot be reduced.

Batch tuning lives in [`host.json`](../host.json) —
`maxEventBatchSize`, `prefetchCount`, `batchCheckpointFrequency` — and can be
overridden per instance without a redeploy via
`AzureFunctionsJobHost__extensions__eventHubs__maxEventBatchSize`.

**Set `STATE_BACKEND=redis` for any instance that scales past one worker.**
`memory` behaves identically on one instance; across scale-out two workers count
independently and both can alert.

---

## Security posture

| | |
|---|---|
| **No secrets in configuration** | Event Hub, storage, and Redis are all identity-based. `*_HTTP_AUTH_HEADER` is the one secret, and it is a Key Vault reference. |
| **No keys to rotate** | There is nothing in App settings to expire. |
| **`config/sources.yaml` is gitignored** | Your hub and namespace names are not published. |
| **URLs are redacted in logs** | Query strings on webhook URLs routinely carry a token; `/health` and the startup lines strip them. |
| **`/health` and `/ingest` are `authLevel: FUNCTION`** | They need a function key. Restrict inbound further under **Networking → Access restriction** if they should not be internet-reachable at all. |
| **Least privilege** | Data-plane roles only: Event Hubs Data **Receiver**, Storage Blob Data **Contributor**, Redis Cache Contributor. No control-plane rights anywhere. |
