# Making the repo the sole contributor to the Function App

The goal: **the DevOps repo is the only thing that writes to the Function App
resource, and a merge to `main` is live within minutes.**

Short answer: **the code is ready; the setup isn't yet.** Nothing in the engine
blocks this — the artifact is deterministic, config is versioned, and the tests
gate the environment seam. What's missing is ownership: today three different
things can write to that resource, and two of them will fight.

This page lists exactly what to close, in the order it matters.

---

## What was already ready

| | Why it matters for CD |
|---|---|
| **One deterministic artifact** | `package_function.py --stage` builds the whole app root — code *and* `config/` — from the repo. Same commit in, same folder out. |
| **Detections deploy separately** | They live in another repo and hot-reload from Blob. A detection change is not a function deploy, so the two release cadences don't block each other. |
| **The environment seam is tested** | `tests/test_poc_backends.py` asserts the in-process and Redis state backends produce identical results. A change that breaks POC↔prod equivalence fails the build. |
| **Deploy auth doesn't need basic auth** | `AzureFunctionApp@2` uses the service connection's identity, so you can disable SCM basic auth entirely — which is itself one of the locks below. |

---

## What had to change (done)

### 1. Two owners of app settings

**This was the real blocker.** Terraform declared 23 app settings, and
`app_settings` is a *whole-map* property: a `terraform apply` deletes anything
not in its map. So a pipeline that sets `LOG_TYPE_FIELD` would have it silently
removed by the next infra run, and the pipeline would put it back — forever,
with the winner decided by whoever ran last.

Fixed by splitting ownership cleanly:

- **Terraform owns the resource** and seeds the settings only it knows (endpoints
  and identity ids of things it just created), then stops caring —
  `lifecycle { ignore_changes = [app_settings] }` in
  [infra/modules/function_app/main.tf](../../infra/modules/function_app/main.tf).
- **The app repo owns the settings** from then on, declared in
  [config/appsettings/](../../config/appsettings/) and applied on every deploy.

### 2. Settings weren't in the repo at all

You'd been setting them by hand in the portal. Under "sole contributor" a portal
edit is invisible drift, and nothing would ever detect it.

Now `config/appsettings/<env>.json` is the source of truth, applied by
[tools/poc/apply_settings.py](../../tools/poc/apply_settings.py) on every run.
`${NAME}` placeholders come from pipeline variables, so secrets and per-tenant
values stay out of git while the *shape* is versioned.

Diffing `poc.json` against `prod.json` is now the entire POC→production change,
and it's all values.

Run it yourself before trusting it:

```bash
python tools/poc/apply_settings.py --env poc -g <rg> -n pyre --dry-run \
  --var STORAGE_ACCOUNT=<storage> --var EVENTHUB_NAME=<hub>
```

**Drift is reported, not deleted.** Undeclared settings are listed so you decide:
adopt them into the JSON, or remove them. `--prune` deletes them once you trust
the list. Auto-deleting by default is how you remove a platform setting you
didn't know mattered and take the app down at 2am.

### 3. Nothing verified the deploy

A wrong `LOG_TYPE_FIELD` or a bad bundle pointer deploys *successfully* and
leaves detection silently dead. With CD that's worse, because nobody is watching
each release.

[deploy-function.yml](../../.azure-pipelines/deploy-function.yml) now ends with a
health gate that fails the run if `/health` doesn't return 200 **or** returns 200
with `detections: 0` — up but blind is still broken.

### 4. No deployment pipeline existed

`ci.yml` only tested. `deploy-function.yml` is the missing half: verify → stage →
apply settings → deploy → health gate, with `lockBehavior: sequential` so two
merges can't race on one resource.

Note the `paths:` filter — detections and docs don't trigger it. A README typo
should not restart the detection engine.

---

## What you still have to do (not code)

These are the parts "sole contributor" means that a pipeline can't enforce by
itself.

### Lock out the other writers

A pipeline being *able* to deploy doesn't stop anyone else from doing so.

1. **Disable SCM basic auth.** Function App → Configuration → *SCM Basic Auth
   Publishing Credentials* → **Off**. This kills portal zip upload, FTP, and
   local `func publish`. (It's very likely already off — that's why your zip
   upload was failing.)
2. **Remove standing write access.** Humans get **Reader** on the resource;
   only the pipeline's service principal gets Contributor. Use PIM for
   break-glass rather than permanent rights.
3. **Protect `main`.** Branch policy requiring a PR and a green `ci.yml`.
   Otherwise "sole contributor" just moves the unreviewed change into git.
4. **Decide about `--prune`.** Until you turn it on, drift is reported and
   ignored. That's the right default while you're still discovering which
   settings the platform put there — but it means the repo isn't yet *enforcing*
   configuration, only asserting it.

### Redis, before you turn CD on

This is the one I'd genuinely hold the line on.

Every deploy restarts the workers, and with `STATE_BACKEND=memory` a restart
**wipes dedup counters, thresholds and the redelivery guard**. Deploy a few times
a day and you get:

- alerts that already fired firing again after each release
- thresholds restarting from zero, so a `Threshold: 5` detection may never reach
  5 if deploys land inside its window
- the at-least-once redelivery guard forgetting what it processed

None of that is a CD bug — it's the documented trade of in-process state, and
it's invisible while you deploy by hand once a week. Continuous deployment is
exactly the workload that makes it visible.

`STATE_BACKEND=redis` plus `REDIS_HOST` fixes it, and `prod.json` already has
both. **Treat Redis as a prerequisite for CD, not a follow-up.**

### Decide how you roll back

Redeploying the previous commit is the honest answer, and it works because the
artifact is deterministic. Two things make it reliable:

- Keep settings out of the deploy artifact (they already are), so rolling back
  code doesn't silently roll back configuration.
- Keep old bundle zips in the `detections` container. Rolling back *detections*
  is re-uploading a `current.json` naming an older bundle — no function deploy at
  all.

---

## Readiness checklist

| | Status |
|---|---|
| Deterministic artifact from the repo | ✅ |
| App settings versioned in the repo | ✅ |
| Terraform and pipeline no longer fight over settings | ✅ |
| Deployment pipeline with tests + health gate | ✅ |
| Concurrency lock | ✅ |
| Path filter (detections/docs don't redeploy) | ✅ |
| SCM basic auth disabled | ⬜ you |
| Human write access removed | ⬜ you |
| `main` branch protection | ⬜ you |
| **Redis before enabling CD** | ⬜ you — see above |
| Settings drift *enforced* (`--prune`) | ⬜ optional, once the drift list is clean |

The four unchecked infrastructure items are all portal or policy changes. The
Redis one is the only one that changes behaviour, and it's the only one I'd call
a blocker rather than a nice-to-have.
