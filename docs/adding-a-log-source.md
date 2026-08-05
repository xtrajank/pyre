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
on (it has to be, for the detection bundle and the output destinations). Reuse it:

Event Hubs **Namespace** → **Access Control (IAM)** → **+ Add → Add role
assignment** → role **Azure Event Hubs Data Receiver** → **Managed identity** →
your Function App → **Review + assign**.

That's the only permission this namespace needs, and it's read-only. Up to 5
minutes to apply.

The trigger also writes its checkpoints to the app's own storage account, so it
depends on **Storage Blob Data Contributor** there too — already granted in
[deploying step 2](deploying.md#2-identity-and-roles),
and nothing to repeat per namespace.

> **User-assigned identity?** Check which kind you have — the Identity blade has
> two tabs, and this is the one thing that changes on every step below. If
> **System assigned** is Off and the identity lives under **User assigned**,
> then in the picker above choose subtype **User-assigned managed identity** and
> select *the identity*, not the Function App. Selecting the Function App there
> assigns the role to a system-assigned identity that doesn't exist, and the
> assignment silently covers nothing. Step 2 then needs one extra setting, and
> [deploying](deploying.md#2-identity-and-roles) step 2's storage role needs the same treatment.

### 2. Pick a namespace label and add the app settings

The label is yours — short, and it only has to be unique within
`sources.yaml`. It becomes both the app-setting name and part of every
function name it covers, e.g. `network` → `EVENTHUB_NETWORK`. Anything that
isn't a letter or digit becomes `_` on the way (`app-logs` →
`EVENTHUB_APP_LOGS`), since neither an app-setting name nor an Azure
function name can hold a hyphen.

**That rewriting applies to the setting's name, never to its value.** The
`__fullyQualifiedNamespace` below is the literal hostname from Event Hubs
Namespace → **Overview → Host name**, hyphens intact
(`pyre-evnthub.servicebus.windows.net`). Nor does the label have to equal the
real namespace's resource name — it's a nickname, and only `sources.yaml` and
the app settings ever see it.

Function App → **Settings → Environment variables → App settings** → **+ Add**,
twice:

| Name | Value |
|---|---|
| `EVENTHUB_<LABEL>__fullyQualifiedNamespace` | `<your-namespace>.servicebus.windows.net` |
| `EVENTHUB_<LABEL>__credential` | `managedidentity` |
| `EVENTHUB_<LABEL>__clientId` | **user-assigned identities only** — the identity's Client ID |

**Apply**, confirm the restart. None of these is a secret — there is nothing
here to rotate, leak, or put in a vault.

`__clientId` is per connection because the host resolves each connection's
identity independently; the app-wide `AZURE_CLIENT_ID` is the *worker's* and
does not reach the triggers. Omit it on a system-assigned app, where there is
only one identity to mean. Get the value from Function App → **Settings →
Identity → User assigned** → the identity → **Overview → Client ID** — the same
value for every namespace, since it's the identity being named, not the hub.

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

Set `log_type_field: ""` for a source with no field like that at all —
Azure-native diagnostic logs (Function App logs, Storage Account logs) carry
no Cribl-style dataset field, and a table often holds several categories you
would rather keep grouped as one detection surface anyway. Every record from
that source then routes on its `hub` name instead; write `LogTypes:` in your
detections to match the hub, not a field value.

### 4. Deploy, then verify

```powershell
python -m pytest tests -q      # catches a typo'd/duplicate namespace or hub before it ships
```

Deploy (VS Code, or your pipeline — see [deploying § Deploying from a pipeline](deploying.md#deploying-from-a-pipeline)).
Then `GET /health`:

- The new source appears under `sources`, with the `function` name it
  registered as, the `connection` app setting its trigger will look for, and the
  `consumer_group` it will claim. Compare all three against what you created.
- `problems` is `[]`. A missing or mismatched namespace app setting is named
  there in words — fix that before chasing anything else.

**Then confirm the listener attached**, which `/health` cannot tell you — it
reports configuration, not connections. Restart the app and check Storage
account → **Containers → `azure-webjobs-eventhub`** for a new
`<namespace>.servicebus.windows.net/<hub>/<consumer-group>/ownership/…` path,
whose **Last modified** keeps advancing. That path is the host's own record that
it authenticated, found the hub and claimed partitions. If it never appears,
the log stream at startup names the reason:
[troubleshooting § Is the trigger actually listening?](troubleshooting.md#is-the-trigger-actually-listening).

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
  refuses two sources that would otherwise land on the same function. **Create
  the consumer group first** (hub → **Entities → Consumer groups → + Consumer
  group**): naming one that doesn't exist stops that trigger from starting.

See [troubleshooting.md § Is the trigger actually listening?](troubleshooting.md#is-the-trigger-actually-listening)
if a source is deployed but nothing arrives.
