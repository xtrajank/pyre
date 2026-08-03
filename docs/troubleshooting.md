# Troubleshooting

- [Deploy fails with 403, or "failed to fetch"](#deploy-fails-with-403-or-failed-to-fetch)
- [The functions list is empty after a successful deploy](#the-functions-list-is-empty-after-a-successful-deploy)
- [The Event Hub trigger never fires](#the-event-hub-trigger-never-fires)
- [`/health` says bundle-load-failed](#health-says-bundle-load-failed)
- [Detections load but nothing ever alerts](#detections-load-but-nothing-ever-alerts)
- [Signals appear but alerts don't](#signals-appear-but-alerts-dont)
- [Nothing appears in pyre-output](#nothing-appears-in-pyre-output)
- [A published detection didn't go live](#a-published-detection-didnt-go-live)
- [`/health` or `/ingest` returns 401 or 403](#health-or-ingest-returns-401-or-403)

---

## Deploy fails with 403, or "failed to fetch"

Both symptoms — VS Code's **403**, and the portal's zip upload failing to
fetch — usually have the same cause, and it is not the one people check first.

### The cause, in one sentence

**Every deploy method goes through `https://<app>.scm.azurewebsites.net`, and the
SCM site has its own access restrictions, separate from the ones you set on the
app.** Allowing your IP on the main site does not allow it on SCM.

### Fix it

Function App → **Settings → Networking → Public network access** → **Access
restriction**. There are **two tabs**:

| Tab | Governs |
|---|---|
| **Site access** / your app's hostname | the running functions |
| **Advanced tool site** / `<app>.scm.azurewebsites.net` | **deploys** ← this one |

On the **Advanced tool site** tab, either:

- tick **"Use main site rules"**, so your existing IP allow-list applies to
  deploys too, or
- add an explicit **Allow** rule for your IP.

Watch for the implicit rule at the bottom of the list: once *any* Allow rule
exists, the default action becomes **Deny**, so an empty SCM rule list plus one
main-site rule means SCM denies everything.

That is the fix in the large majority of cases. Re-try the deploy immediately —
no restart needed.

### If it's still failing, in order of likelihood

| Symptom | Cause | Fix |
|---|---|---|
| **401** (not 403) from the portal's Zip Push Deploy | **SCM basic auth is disabled**, which enterprise policy routinely does. Kudu's upload page authenticates with basic auth. | Deploy from **VS Code** instead — it authenticates with your Entra identity through ARM, not basic auth. Or Function App → **Settings → Configuration** → *SCM Basic Auth Publishing Credentials* → **On**. |
| **403** from VS Code, SCM rules already open | Your Azure role. Fetching publish credentials needs **Contributor** or **Website Contributor** on the app; **Reader** is not enough. | Check Function App → **Access control (IAM) → View my access**. |
| Portal upload page won't load at all / "failed to fetch" | The **portal** page for zip deploy talks to SCM **from your browser**. A corporate proxy, or `Public network access: Disabled`, blocks it. | Same SCM access-restriction fix; or use VS Code / a pipeline, which don't go through your browser. |
| No Kudu / Advanced Tools at all | The app is on **Flex Consumption**, which has no Kudu site. Portal zip push doesn't exist there. | Deploy from VS Code or a pipeline. Both use the Flex deploy API. |
| Deploy succeeds, app doesn't change | `WEBSITE_RUN_FROM_PACKAGE` pointing at an old package, or a stale deployment slot. | Function App → **Deployment Center → Logs** — confirm the deployment you just made is the active one. |
| Works from home, 403 at the office (or vice versa) | Your egress IP changed. | This is the argument for deploying from a pipeline instead of a laptop. |

### The route that avoids all of this

**Deployment Center → Azure Repos**, or the pipeline in
[azure-pipelines.yml](../azure-pipelines.yml). The build and the upload both
happen inside Azure, so nothing has to reach SCM from your machine or your
office network. Given you're moving this repo into an internal ADO repo anyway,
that's the destination — see [prod.md § Deploying](prod.md#5-deploying).

> **Deployment Center setup:** Function App → **Deployment Center** → Source:
> **Azure Repos** → pick org/project/repo/branch `main` → Build provider: Azure
> Pipelines → Save. It writes a pipeline into the repo and deploys on every push.
> The repo root is already the Function App root, so there's nothing to
> configure.

---

## The functions list is empty after a successful deploy

The deploy landed; `function_app.py` raised while importing, so no function was
registered. Azure reports this only in the log stream.

**Monitoring → Log stream**, then restart the app and read the first 20 lines.

| In the log | Meaning |
|---|---|
| `ModuleNotFoundError: No module named 'yaml'` | The remote build didn't run. Redeploy with `SCM_DO_BUILD_DURING_DEPLOYMENT=true` and `ENABLE_ORYX_BUILD=true` in app settings — or from VS Code, which sets them. |
| `ValueError: config/sources.yaml: source ... has unknown key(s)` | A typo in `sources.yaml`. The message names the key. |
| `ValueError: ... every source needs a hub:` | A `sources.yaml` entry with no `hub:`. |
| `The listener for function 'detect_x' was unable to start` | The hub named in `sources.yaml` doesn't exist in that namespace, or the connection setting is wrong or missing. See below. |
| Nothing at all | Python version mismatch. The app must be **Python 3.11** (Settings → Configuration → Stack). |

Catch every one of these before deploying:

```powershell
python -m pytest tests -q
```

The suite imports `function_app.py` exactly as the worker does and asserts the
three functions register.

---

## The Event Hub trigger never fires

`/health` is fine, `ingest` produces signals, but real logs never arrive.

**1. Is the hub receiving anything?** Event Hubs Namespace → your hub → **Data
Explorer → View events**. Nothing there means the problem is upstream — check
the diagnostic setting or the sender, not pyre.

**2. Does the hub name match exactly?** `/health` lists each source's `hub`.
Compare it character by character with the hub in the portal.

**3. Is the connection setting right?** The name in `sources.yaml` (`connection:`,
default `EVENTHUB_CONNECTION`) must exist as an app setting, in one of two
shapes:

```
# connection string
EVENTHUB_CONNECTION = Endpoint=sb://<ns>.servicebus.windows.net/;SharedAccessKeyName=...

# or identity-based (preferred - no secret)
EVENTHUB_CONNECTION__fullyQualifiedNamespace = <ns>.servicebus.windows.net
EVENTHUB_CONNECTION__credential              = managedidentity
```

A connection string scoped to a *specific hub* (`;EntityPath=...`) only works for
that hub. Use a namespace-level one.

**4. Identity-based? Check the role.** The Function App's identity needs **Azure
Event Hubs Data Receiver** on the namespace. Up to 5 minutes to apply.

**5. Is something else already consuming `$Default`?** Two consumers on one
consumer group fight over the lease. Create a consumer group for pyre and name it
in `sources.yaml` (`consumer_group: pyre`).

**6. Diagnostic logs are batched by Azure**, up to ~5 minutes from event to
delivery. Slow is not the same as broken.

---

## `/health` says bundle-load-failed

The `error` field names the exception.

| Error | Cause |
|---|---|
| `ResourceNotFoundError` / `BlobNotFound` | `current.json` isn't at the **root** of the `detections` container, or the container name doesn't match `DAC_CONTAINER`. |
| `ClientAuthenticationError` / `AuthorizationPermissionMismatch` | The app's identity lacks **Storage Blob Data Contributor** on the storage account. Assign it and wait 5 minutes. |
| `ServiceRequestError` / DNS failure | `DAC_BLOB_ACCOUNT_URL` is malformed. It is exactly `https://<account>.blob.core.windows.net` — no trailing slash, no container, no `?` parameters. |
| `KeyError: 'version'` | `current.json` isn't the expected JSON. It must be `{"version": "...", "path": "bundles/....zip"}`. |
| `BadZipFile` | The zip didn't upload completely, or `path` points at something that isn't the bundle. |

`"detections": 0` with `"status": "no-detections-loaded"` is different: the
bundle loaded and contained nothing usable. Re-run `python publish.py` and read
its validation output — it names every file it rejected and why.

---

## Detections load but nothing ever alerts

Almost always routing, and `/health` plus the log stream will tell you which of
the two it is.

**Compare these two things:**

1. `/health` → `log_types` — what your detections declare.
2. The actual value in your records under `sources[].log_type_field`.

They must be **exactly** equal. `RuntimeAuditLogs` ≠ `runtimeauditlogs`.

Then send a batch and read **Log stream**:

```
4 event(s) from hub 'logs-in' had no value in the log-type field 'category'
- check log_type_field in config/sources.yaml against your data
```

→ `log_type_field` names a field your records don't have. Usually casing
(`category` vs `Category`). Fix it in `sources.yaml` and redeploy.

```
no detections are registered for these log-type values: OperationalLogs (3 event(s)).
```

→ Routing worked; no detection covers that value. Either add one, or accept the
gap. This line is your coverage report — worth an Application Insights alert.

**Third possibility: the envelope.** If records are wrapped in `{"records": [...]}`
and `envelope_field` is empty, the engine evaluates the *envelope* as one event —
which has no `category` field, so you get the first message above. And the
reverse: `envelope_field: records` against flat records is harmless (the engine
only unwraps when the field holds a list).

**Fourth: the rule genuinely doesn't match.** Test it on your laptop against a
real record:

```powershell
python tools/run_local.py --bundle ..\my-detections --file real-sample.json `
  --log-type-field category --event-time-field time
```

---

## Signals appear but alerts don't

**This is usually correct behaviour, not a fault.** An alert requires the match to
also:

1. clear the detection's `Threshold:` — N matches sharing a dedup string, and
2. not duplicate an alert already open within `DedupPeriodMinutes:`.

Check the detection's YAML. `Threshold: 3` with three matches spread across three
different `dedup()` values produces three counts of one, and no alert.

Also check `CreateAlert:` isn't `false` — that's the "record it but never page"
setting.

To confirm dedup is what's holding it: restart the Function App (which clears
in-process state) and re-send. If an alert appears, the earlier one was already
open.

---

## Nothing appears in pyre-output

| Check | |
|---|---|
| Is anything matching at all? | If `signals/` is empty too, this is a routing problem — see above. |
| `OUTPUT_BLOB_ACCOUNT_URL` set? | `/health` → `output` shows what the engine resolved. `null` means neither output setting is set and records are being **dropped**. |
| Is `OUTPUT_HTTP_URL` set? | It wins over the blob. Unset it if you want blob output. |
| Blob role assigned? | **Storage Blob Data Contributor**. Without it, App Insights shows `append-blob write failed`. |
| Right container? | Default `pyre-output`; `OUTPUT_BLOB_CONTAINER` overrides. The engine creates it if it can. |

Write failures are logged and swallowed on purpose — a failed write must not fail
the batch, or Event Hubs redelivers it and the alert fires twice. So **check
Application Insights**, not just the container:

```kusto
traces | where message contains "write failed" or message contains "POST failed"
```

---

## A published detection didn't go live

1. **Did the version change?** Workers reload when, and only when, the `version`
   in `current.json` changes. `publish.py` hashes the file contents, so it
   changes automatically — unless you passed `--version` and reused a value.
2. **Did both files upload?** The zip **and** `current.json`, with `path`
   matching where the zip actually is, `bundles/` included.
3. **Has the refresh interval elapsed?** Up to `DAC_REFRESH_SECONDS` (default 60)
   on a warm worker.
4. **Check `/health` → `bundle_version`.** If it still shows the old value after
   a couple of minutes, the pointer read isn't seeing your upload — confirm the
   container and account in `DAC_BLOB_ACCOUNT_URL` / `DAC_CONTAINER`.
5. **A reload can fail silently by design.** If the new bundle can't be read, the
   worker keeps serving the last good one rather than stopping detection. App
   Insights: `bundle refresh failed`.

---

## `/health` or `/ingest` returns 401 or 403

**401** — missing or wrong function key. Get the full URL including the key from
Function App → **Overview → Functions → health → Get function URL**. Keys are
per-function.

**403** — network restrictions on the **main** site this time (not SCM). Function
App → **Settings → Networking → Access restriction → Site access**. Add your IP,
or check whether an existing Allow rule has made the default action Deny.
