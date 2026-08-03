# Adding a log source

The checklist for onboarding one more Event Hub — a brand new namespace, or one
more hub in a namespace pyre already reads. No engine change either way, and no
secret is ever created or stored.

**Prerequisite, out of scope for this guide:** the namespace exists and can
reach the Function App (same VNet, a private endpoint, or public access —
whatever your network already requires for the other namespaces pyre reads).

---

## A new namespace

### 1. Grant read access — no connection string, ever

Function App → **Settings → Identity** confirms a managed identity is already
on (it has to be, for the DAC blob and output). Reuse it:

Event Hubs **Namespace** → **Access Control (IAM)** → **+ Add → Add role
assignment** → role **Azure Event Hubs Data Receiver** → **Managed identity** →
your Function App → **Review + assign**.

That's the only permission this namespace needs, and it's read-only. Up to 5
minutes to apply.

### 2. Pick a namespace label and add the app settings

The label is yours — short, and it only has to be unique within
`sources.yaml`. It becomes both the app-setting name and part of every
function name it covers, e.g. `network` → `EVENTHUB_NETWORK`.

Function App → **Settings → Environment variables → App settings** → **+ Add**,
twice:

| Name | Value |
|---|---|
| `EVENTHUB_<LABEL>__fullyQualifiedNamespace` | `<your-namespace>.servicebus.windows.net` |
| `EVENTHUB_<LABEL>__credential` | `managedidentity` |

**Apply**, confirm the restart. Neither value is a secret — there is nothing
here to rotate, leak, or put in a vault.

### 3. Add the block to `sources.yaml`

```yaml
namespaces:
  - namespace: network                              # matches EVENTHUB_NETWORK above
    fully_qualified_namespace: your-namespace.servicebus.windows.net   # optional - checked by /health
    hubs:
      - hub: palo-traffic-in
        log_type_field: dataset       # whatever field routes on THIS feed
        event_time_field: _time
        envelope_field: ""            # "" if one message = one record
```

Only `namespace:` and `hub:` are required; the rest default to Azure diagnostic
settings' own shape (`category` / `time` / `records`) — check yours in Event
Hubs Namespace → your hub → **Data Explorer → View events → Body** before
assuming the defaults fit.

### 4. Deploy, then verify

```powershell
python -m pytest tests -q      # catches a typo'd/duplicate namespace or hub before it ships
```

Deploy (VS Code, or your pipeline — see [prod.md § Deploying](prod.md#5-deploying)).
Then `GET /health`:

- The new source appears under `sources`, with the `function` name it
  registered as.
- `eventhub_settings` is `[]`. Anything else names exactly which namespace's
  app setting is missing or mismatched — fix that before chasing anything
  else.

Optionally prove the detection side before real traffic arrives:

```powershell
Invoke-RestMethod -Method Post -Uri "<ingest-function-url>&source=network/palo-traffic-in" `
  -ContentType "application/json" -InFile sample.json
```

---

## One more hub in an existing namespace

Steps 1–2 above are already done for that namespace. Just add a `hub:` entry
under the existing `namespace:` block (step 3) and deploy (step 4). That's the
entire reason hubs nest under their namespace instead of repeating a
`connection:` field each — one namespace onboarded is every hub in it one line
away.

---

## Why this can't collide

- **Function name.** Derived as `detect_<namespace>_<hub>`, so two namespaces
  that happen to use the same hub name (very common — Azure diagnostic
  settings default to names like `insights-logs-signinlogs` everywhere) can
  never produce the same Azure function. `load_sources()` also rejects any
  accidental duplicate at config-load time, before a deploy ever runs.
- **Connection setting.** Named from `namespace:`, not typed per hub — every
  hub under one namespace block shares it, so there's no per-hub string to
  mistype onto the wrong namespace.
- **Two triggers on one hub.** If something else already consumes `$Default`
  on a hub, give your entry its own `consumer_group:` — `load_sources()`
  refuses two sources that would otherwise land on the same function.

See [troubleshooting.md § The Event Hub trigger never fires](troubleshooting.md#the-event-hub-trigger-never-fires)
if a source is deployed but nothing arrives.
