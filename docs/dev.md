# dev — where changes get tested

dev exists to answer one question before prod does: **does this change break
anything?** It is prod's shape at a fraction of prod's size, and the only rule
that matters is that **nothing reaches prod without going through here first**.

If you have done [poc.md](poc.md), you have already built a dev environment. The
difference is what you do with it.

---

## Resources

Ask for these in their own resource group, separate from the POC and separate
from prod:

| Resource | Notes |
|---|---|
| Event Hubs namespace + hub(s) | Standard tier, 1–2 partitions per hub. Mirror prod's `namespace:` labels and hub *names* so `config/sources.yaml` differs from prod's only in which app settings those labels resolve to. |
| Storage account | Containers `detections` and `pyre-output`, both Private. |
| Function App | Linux, Python 3.11, Consumption. System-assigned identity On. |
| Application Insights | On the Function App. This is where you read logs. |

The identity needs **Storage Blob Data Contributor** on the storage account, and
**Azure Event Hubs Data Receiver** on the namespace if you're using
identity-based Event Hub connections. Same as
[poc.md Step 2](poc.md#step-2--give-the-function-app-access-to-that-storage).

No Redis in dev. One instance, in-process state, and the difference from prod is
[documented and understood](prod.md#shared-state).

---

## App settings

Identical to the POC except the label and the refresh:

| Setting | dev |
|---|---|
| `PYRE_ENV` | `dev` |
| `EVENTHUB_<NAMESPACE>` per namespace in `sources.yaml` | identity-based, pointing at the dev namespace(s) — see [adding-a-log-source.md](adding-a-log-source.md) |
| `DAC_BLOB_ACCOUNT_URL` | `https://<dev-storage>.blob.core.windows.net` |
| `DAC_REFRESH_SECONDS` | `30` — fast feedback matters more than cost here |
| `OUTPUT_BLOB_ACCOUNT_URL` | `https://<dev-storage>.blob.core.windows.net` |
| `AzureFunctionsJobHost__extensions__eventHubs__maxEventBatchSize` | `100` |

`OUTPUT_HTTP_URL` stays unset: signals and alerts land in blobs you can read, so
a dev change can't pollute the real SIEM. Set `ALERT_WEBHOOK_URL` only if you
have a dedicated dev channel to page.

---

## The two things dev is for

### 1. Testing an engine change

```powershell
python -m pytest tests -q          # 42 tests, ~4 seconds, no Azure
python tools/run_local.py          # the whole loop on your laptop
```

Both of those run with no cloud at all, so most engine changes never need dev.
When one does — anything touching triggers, identity, or blob access — deploy the
branch to dev from VS Code exactly as in
[poc.md Step 6](poc.md#step-6--deploy-the-engine), then:

1. `/health` — three functions, right bundle, right `log_type_field` per source.
2. `ingest` a known payload — the signals and alerts you expect appear.
3. Confirm the listener attached — `azure-webjobs-eventhub` in the storage
   account has a live `ownership/` blob for each hub
   ([how, and what else to check](troubleshooting.md#is-the-trigger-actually-listening)).
   `/health` reports configuration, not connections, so this is a separate step.
4. Send through the real hub — the trigger fires and checkpoints.

Only then merge to `main`.

### 2. Testing a detection change

This is the common case, and it should never involve deploying anything.

```powershell
cd dac
python publish.py --upload https://<dev-storage>.blob.core.windows.net
```

Thirty seconds later dev is running the new rule. Send it real log samples via
`ingest` and read `pyre-output/signals/`. When you're happy, merge — the pipeline
publishes to prod behind an approval.

**Test a rule against real data before it ever reaches Azure:**

```powershell
python tools/run_local.py --bundle ..\my-detections --file real-sample.json `
  --log-type-field category --event-time-field time
```

That is the same engine, so a rule that fires here fires in Azure.

---

## Promotion

```
laptop            dev                        prod
──────            ───                        ────
pytest      ─┐
run_local.py ├──▶ publish.py --upload   ──▶  pipeline + approval
                  deploy from VS Code   ──▶  pipeline + approval
```

Two pipelines do this once you're on Azure DevOps, and both wait for an approval
on the `prod` environment:

- [azure-pipelines.yml](../azure-pipelines.yml) — the engine
- [dac/azure-pipelines.yml](../dac/azure-pipelines.yml) — the detections

Until they're wired up, promotion is: deploy from VS Code to dev, verify, then
deploy to prod.

---

## Keeping dev honest

A dev environment nobody trusts gets skipped, which is worse than not having one.
Three habits:

- **Same detections repo, same branch flow.** dev publishes from `main` of the
  detections repo just like prod does; it is *ahead*, not *different*.
- **Real log shapes.** Keep a folder of real (scrubbed) message bodies from each
  source and replay them through `ingest` after any change. If a source's records
  change shape, this is where you find out.
- **Delete drift.** If you change an app setting in the dev portal to make
  something work, change it in prod's checklist too — or it isn't tested.

**Next:** [prod.md](prod.md).
