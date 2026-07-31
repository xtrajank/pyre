# POC → production: what actually changes

The short answer: **no engine code, and no bundler code.**

Going to production is three configuration changes and one new pipeline. This
page lists them exactly, and — more usefully — explains what in the design makes
that true, so it stays true as the system grows.

---

## The whole diff

| # | Change | Where | Code change? |
|---|---|---|---|
| 1 | State moves from in-process to Redis | app setting `STATE_BACKEND=redis` + `REDIS_HOST` | none |
| 2 | Signals/alerts move from blobs to the lake | app setting `SIGNALS_SINK_URL` | none |
| 3 | Alerts start opening cases | `config/destinations.yaml` + `DEFAULT_ROUTES=torq_prod` | none |
| 4 | All hubs get a trigger, not just one | `config/sources.yaml` (or `EVENTHUB_NAMES`) | none |
| 5 | The DaC is published by CI, not by hand | a new pipeline calling the **same** `bundle.py` | none |
| 6 | The function is deployed by CI, not from VS Code | a new pipeline calling the **same** `--stage` | none |
| 7 | App settings come from the repo, not the portal | `config/appsettings/<env>.json` | none |

Making the repo the **sole** writer to the resource needs a few things a pipeline
can't enforce on its own — see
**[continuous-deployment.md](continuous-deployment.md)**.

Nothing in `engine/pyre_engine/` outside `backends/` is touched, and `backends/`
only ever gains a module — never an edit to an existing one. `bundle.py` is
byte-for-byte identical; only its trigger changes.

---

## Why it's only configuration

### One seam, two things behind it

Every environment-dependent decision lives in
[engine/pyre_engine/backends/](../../engine/pyre_engine/backends/), and there are
only two:

```
                  ┌──────────────────────────────────────────────┐
                  │  processor.py                                │
                  │  route → rule() → signal → threshold/dedup    │  ← identical
                  │  → alert → dispatch                          │     everywhere
                  └───────┬──────────────────────────┬───────────┘
                          │ StateStore               │ sink.write(records)
          ┌───────────────┴────────┐      ┌──────────┴──────────────┐
          │  backends/             │      │  backends/              │
          │  memory_state  (POC)   │      │  blob_sink   (POC)      │
          │  redis_state   (prod)  │      │  http_sink   (prod)     │
          └────────────────────────┘      └─────────────────────────┘
                     STATE_BACKEND              SIGNALS_SINK_URL /
                                                OUTPUT_BLOB_ACCOUNT_URL
```

The processor holds a `StateStore` and a sink. It never learns which
implementation it got. That's not a convention to remember — it's enforced by
[tests/test_poc_backends.py](../../tests/test_poc_backends.py), which asserts the
same detection results across both.

### Both state backends share their semantics

`StateStore` in [state.py](../../engine/pyre_engine/state.py) owns the key names,
the TTL rules and what is atomic. Only the *client* underneath swaps. So the two
backends cannot drift: there is no second copy of the dedup logic to keep in
sync. The only real difference is whether state is shared between workers —
which is the entire reason Redis exists.

### Both sinks take the same records

Signals and alerts are built once, in
[signals.py](../../engine/pyre_engine/signals.py), and every record is
self-describing:

```json
{"p_record_type": "signal", "p_signal_id": "…", "p_alert_id": "…" | null, …}
{"p_record_type": "alert",  "p_alert_id": "…", …}
```

The sink then lays them out however its medium wants. The lake routes on
`_dataset`; a blob container has no routing layer, so `blob_sink` splits the two
streams into two files itself. Same records, different plumbing — a sink detail,
not a behaviour change.

### Recording an alert is separate from delivering it

Every alert is written to the alerts stream **regardless of routing**. Dispatch —
opening a Torq case — is a separate step that reads `DEFAULT_ROUTES`.

That's why the POC needs no destination at all: it produces a complete, readable
alert history with nothing configured. Turning on Torq later adds a delivery; it
doesn't change what gets recorded.

---

## The one behavioural difference, stated plainly

**In-process state is per worker and resets on a cold start.**

Dedup windows, thresholds, the storm limit and the redelivery guard all live
inside one Python process. With a single instance over a demo, that behaves
identically to Redis. It is not correct across scale-out — two workers would
count independently and could both alert on the same thing.

That is the *only* difference, and `STATE_BACKEND=redis` is the whole fix.

Two smaller consequences of it, both invisible once Redis is in:

- **`blob_sink` dedups alerts by `p_alert_id`.** The processor already claims
  each alert atomically before dispatch, so on Redis a repeat never reaches the
  sink. On in-process state that claim is lost across a restart, and Event Hubs
  is at-least-once — so the sink keeps a seen-set as compensation. Redis makes it
  redundant, not wrong.
- **Signals are never deduped, in either environment.** They're an audit of
  everything that matched; repeats are meaningful and must survive.

---

## Doing it

### 1. State → Redis

```
STATE_BACKEND  = redis
REDIS_HOST     = <cache>.redis.cache.windows.net
REDIS_PORT     = 6380
REDIS_USE_ENTRA = true
```

Grant the app's managed identity the Redis Data Contributor role. No password
anywhere — [redis_state.py](../../engine/pyre_engine/backends/redis_state.py)
refreshes an Entra token per connection, which is what stops a long-warm worker
failing every call once its first token expires.

Remove `OUTPUT_BLOB_ACCOUNT_URL` only if you no longer want the blob copies.

### 2. Signals/alerts → the lake

```
SIGNALS_SINK_URL = https://<cribl-http-source>/…
```

Set it and the sink becomes `http_sink`; leave it unset and blobs continue. The
records are unchanged, so a Cribl route on `_dataset` (or on `p_record_type`)
splits them into `pyre_signals` and `pyre_alerts`.

### 3. Alerts → Torq

In `config/destinations.yaml` (already shipped inside the function package):

```yaml
- name: torq_prod
  kind: torq
  enabled: true
  url_env: TORQ_PROD_URL
  token_env: TORQ_PROD_TOKEN     # a Key Vault reference, never inline
```

```
DEFAULT_ROUTES = torq_prod
TORQ_PROD_URL  = …
TORQ_PROD_TOKEN = @Microsoft.KeyVault(SecretUri=…)
```

A detection can override per alert with `destinations(event)`. The `torq` adapter
already exists in [dispatch.py](../../engine/pyre_engine/dispatch.py) — adding a
*new kind* of destination is one function there; adding an *instance* is config.

### 4. Every hub gets a trigger

List them in `config/sources.yaml`:

```yaml
sources:
  - name: palo_traffic
    log_types: [SEC_Network_Palo_Alto_Traffic]
    hub: palo-traffic-in
    partitions: 32
  - name: cloudflare
    log_types: [SEC_Web_Cloudflare]
    hub: cloudflare-in
    partitions: 16
```

`function_app.py` registers one Event Hub trigger per hub at import time, all
funnelling into the same handler, so twenty hubs is a twenty-line YAML file and
no code. Duplicate hubs collapse to one trigger. `EVENTHUB_NAMES` (comma
separated) overrides the file if you'd rather not ship it.

The same file is what Terraform sizes the hubs from, so a source is onboarded in
one place. Also raise the batch size for real volume:

```
AzureFunctionsJobHost__extensions__eventHubs__maxEventBatchSize = 256
```

### 5. The DaC publishes itself

The bundler does not change. `dac_bundler/bundle.py` already produces exactly
what the engine reads; the only difference is what runs it and what uploads the
result.

```yaml
# in your DaC repo, on push to main
- script: python dac_bundler/bundle.py
- task: AzureCLI@2
  inputs:
    inlineScript: |
      az storage blob upload-batch -d detections -s dist/bundles \
        --destination-path bundles --account-name $(STORAGE) --auth-mode login --overwrite
      az storage blob upload -c detections -n current.json -f dist/current.json \
        --account-name $(STORAGE) --auth-mode login --overwrite
```

**The upload order is load-bearing**: bundle first, pointer last, so a worker can
never read a pointer to a bundle that isn't there. The manual portal steps are
the same two uploads in the same order — the pipeline just automates a sequence
you've already proven by hand.

Give the pipeline's service connection **Storage Blob Data Contributor** on the
account. No PAT reaches the function; it reads the bundle via managed identity.

Because `bundle.py` validates and exits non-zero, a broken detection fails the
build instead of silently publishing a bundle that loads nothing.

### 6. The function deploys itself

Same principle as the bundler: the artifact doesn't change, only what produces
it. `--stage` builds the identical folder VS Code deploys, so a pipeline is the
same two steps you already ran by hand.

```yaml
# in this repo, on push to main
trigger:
  branches: { include: [main] }

steps:
  - task: UsePythonVersion@0
    inputs: { versionSpec: '3.11' }

  - script: |
      pip install -r engine/requirements.txt pytest fakeredis
      python -m pytest tests -q
    displayName: Test

  - script: python tools/poc/package_function.py --stage
    displayName: Stage the function app root

  - task: AzureFunctionApp@2
    inputs:
      connectedServiceNameARM: $(serviceConnection)
      appType: functionAppLinux
      appName: pyre
      package: dist/functionapp
      runtimeStack: 'PYTHON|3.11'
      deploymentMethod: zipDeploy
```

`AzureFunctionApp@2` authenticates through the service connection's identity, not
SCM basic auth — so it works in tenants where the portal's zip upload doesn't,
for the same reason VS Code does.

Two things worth keeping in the pipeline:

- **Run the tests before deploying.** `tests/test_poc_backends.py` asserts the two
  state backends produce identical results, so a change that breaks the
  environment seam fails the build rather than production.
- **Don't let the pipeline set app settings.** Deployment and configuration
  moving independently is what makes a rollback a redeploy of the previous
  artifact, with no settings archaeology.

---

## What you should still expect to do

Being straight about it — these aren't code changes, but they aren't free:

| Work | Why |
|---|---|
| Stand up Redis, VNet, private endpoints, Key Vault | The POC deliberately has none. `infra/` already defines them. |
| Tune batch size and partitions | Ten events proves nothing about throughput. |
| Point Cribl at the hubs, and normalise | The engine assumes a log-type field already exists; that's Cribl's job. |
| Re-check `LOG_TYPE_FIELD` | Cribl-normalised events won't carry the same field name as raw Azure diagnostic logs. |
| Load-test | Especially dedup-key cardinality in Redis. |
| Decide bundle-version pinning | `pyre pull` stamps the DaC commit sha; pin a tag for reproducible prod. |

---

## Keeping it true

Three rules. If a change needs an exception to one, it belongs in `backends/`.

1. **Nothing outside `backends/` may branch on the environment.** No
   `if env == "poc"`, no `if redis_host`. Grep for `cfg.env` — it should appear
   only as a label.
2. **A new backend is a new module plus one branch** in `build_state_store` or
   `build_record_sink`. Never an edit to an existing backend.
3. **Every environment-dependent choice is a config value**, and both paths are
   exercised by the same test asserting the same result.

The current cost of that discipline is small — two `build_*` functions and four
modules — and it's what turns "port the POC to production" into an afternoon of
settings.
