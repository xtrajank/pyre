# pyre production deployment checklist

Work top to bottom. Everything here was derived from an end-to-end run of the
real engine against real Redis 6.0 and a real offline Terraform plan
(`tools/sim/`, `infra/tests/plan.tftest.hcl`) — each item is something that was
either observed to break or is invisible until it does.

Two rules for reading this:

- **Silent failure is the enemy.** Most items exist because the failure mode is
  "everything looks healthy and no alert ever fires."
- **Don't trust a green plan.** Several blockers below plan clean, apply clean,
  and only fail at runtime.

---

## 0. STOP — before you deploy

- [ ] **`terraform -chdir=infra test` is 14/14.** It is offline (mock provider,
      no subscription, no cost) and it is what proves the hub wiring, the cost
      profile, and the security posture.
- [ ] **`config/detections.yaml` pins `ref: laptop-test`.** A branch pin means
      prod silently changes the next time anyone pushes to it. Pin a **tag or
      commit SHA**.
- [ ] **`./cli/pyre validate` passes.** It currently reports one real error: a
      Panther **Simple Detection** (`github_repo_archived.yml` — YAML-only
      `Detection:` block, no Python). pyre runs Python rules only and would
      **skip it silently**, so validate refuses it. Port it to a `rule(event)`
      `.py`, or drop it via `dac.exclude` and accept the gap knowingly.

> **Fixed — the two blockers that made the system evaluate nothing.** Recorded
> because the shape of the failure is worth remembering:
> `infra/main.tf` hardcoded `eventhub_name = "logs-in"`, a hub
> `config/sources.yaml` has never created — both sides are just strings, so it
> planned green, applied green, and bound the trigger to nothing. And
> `engine/function_app.py` declared a single trigger, so even a correct name
> would have left the other hubs ingesting at full rate with nothing evaluating
> them. Hub names are now derived from `sources.yaml`, the processor registers
> **one trigger per hub**, and `terraform test` asserts both directions (every
> name is real; every hub has a consumer).

---

## 1. Dev vs prod — how to be sure a lab can never page production

Read this before you deploy anything. Routing is **explicit per instance** — a
`default_routes` list in that instance's tfvars, naming destinations declared in
`config/destinations.yaml`:

```hcl
destinations   = { mock = { url = "https://<mock-func>/api/alert" } }
default_routes = ["mock"]
```

It is **not** derived from `env`. It used to be
`["mock"] if env == "dev" else ["torq_prod"]` in the engine, under which *any*
env string that was not exactly `"dev"` — `"Dev"`, `"lab"`, `"staging"`, a typo —
routed to production. That is gone; `env` now only tags resources and sets
`PYRE_ENV`.

- [ ] **A lab instance declares NO production destination.** This is the whole
      guarantee. Omit it from `destinations` and there is no URL and no token, so
      the adapter raises before it can POST anywhere — it fails **closed** and
      **loudly** (a `dispatch_failed` count and an ERROR line), never silently.
- [ ] **`default_routes` names only that instance's own sink** (e.g. `["mock"]`).
- [ ] **Never create a production token secret in a lab Key Vault.** A
      destination's `token_secret` is the only thing that produces a
      `DESTINATION_<NAME>_TOKEN` Key Vault reference; declare no destination and
      no such setting is generated at all.
- [ ] **Do not rely on `enabled:` in `config/destinations.yaml`.** It is not an
      env gate — it controls whether the Dispatcher registers the route *at all*,
      so setting it `false` doesn't mute a lab, it breaks prod (§5).
- [ ] **A detection can override routing.** A DaC rule's optional
      `destinations(event)` takes precedence over `default_routes` entirely. In a
      lab this still fails closed on the missing URL/token — which is exactly why
      "declare no production destination" is the control that matters, rather
      than any config the DaC repo can override.

**Summary of the guarantee:** a lab is safe because it has no production URL and
no production token — *not* because of `env`, and *not* because of the `enabled`
flag. `terraform test` asserts a dev instance publishes no destination settings
beyond its own sink.

### Destinations are generic — nothing is named after a vendor

| Layer | Owns |
|---|---|
| `config/destinations.yaml` | which destinations exist, each one's **`kind`**, and the env var names for its url/token |
| `infra` (`var.destinations`) | the per-instance **values** → `DESTINATION_<NAME>_URL` / `_TOKEN` |
| `dispatch.py` | one adapter per **kind** (`mock` / `webhook` / `torq`) |

So moving off Torq is a `kind` change plus a tfvars value — no Terraform variable
renames, no engine change. Adding a second SOAR is one `destinations.yaml` entry
and one tfvars key.

---

## 1b. The default hub — and the one thing pyre does NOT do

**pyre cannot route an event to a hub.** By the time the engine sees an event it
is already *in* a hub; the hub was chosen by **Cribl** at send time. The engine
reads the log-type field only to pick *detections*. So the catch-all is a
contract between three parties, and pyre owns only two of them:

| Who | Job |
|---|---|
| `config/sources.yaml` | declares the hubs and marks exactly one `default: true` |
| Terraform | creates each hub and subscribes the processor to **all** of them |
| **Cribl** | **must send each log type to its hub, and everything else to the default** |

- [ ] **Configure Cribl's fallback route to the default hub**
      (`terraform output default_eventhub_name`). This is the half pyre cannot
      enforce: a log type Cribl sends to a hub pyre does not consume is
      evaluated by **nothing**, silently. With a fallback configured there is
      always a right answer.
- [ ] Understand what the default hub does and doesn't buy. A log type landing
      there is handled **identically** — same routing by log-type field, same
      detections, same signals, dedup, alerts. A dedicated hub buys
      **isolation** (a firehose can't starve the rest) and its **own
      parallelism ceiling**. It does not buy different treatment.
- [ ] **Watch consumer lag on the default hub.** It is deliberately the cheapest
      (fewest partitions), because it carries the long tail. Partitions are the
      parallelism ceiling, so if a genuinely high-volume source ever falls
      through to it, it will lag. That lag is the signal to give it its own entry
      in `sources.yaml` — nothing else will tell you.
- [ ] `./cli/pyre validate --show-default-routed` lists which log types have no
      dedicated hub. Useful before a big DaC bump.

## 2. Before `terraform apply`

- [ ] `terraform -chdir=infra test` → 14/14 (see §0).
- [ ] `terraform -chdir=infra fmt -check -recursive && terraform -chdir=infra validate`.
- [ ] **Create each destination's `token_secret` in the engine Key Vault FIRST**,
      before apply. Terraform turns `token_secret` into a
      `@Microsoft.KeyVault(SecretUri=...)` reference; if the secret is absent the
      reference does not resolve and every dispatch fails auth at runtime.
      `az keyvault secret set --vault-name <name_prefix>-kv --name <token_secret> --value <token>`
      Note the **engine** vault (`<prefix>-kv`), never the CI vault
      (`<prefix>-ci-kv`) — they are separate on purpose so a compromise of one
      identity cannot read the other's secrets.
- [ ] **Set `destinations` and `default_routes`** for this instance (§1). A
      destination you don't declare simply has no URL and fails closed.
- [ ] Set `signals_sink_url`. It is optional in Terraform and **its absence
      discards every signal and alert record** — the whole audit trail — while
      the system looks healthy. The engine now warns once per worker at cold
      start; alarm on that line (§7).
- [ ] Set `log_sender.principal_id`. Empty means **no Send role is granted and
      Cribl cannot write to Event Hubs at all**.
- [ ] Set `publisher.principal_id` (or `federated_credentials`), or CI cannot
      publish detection bundles.
- [ ] `cost_profile = "scale"` for prod (larger Managed Redis — `Balanced_B5`
      floor, auto-inflate Event Hubs, Flex to 1000 instances). `test` is Managed
      Redis `Balanced_B0` — the smallest SKU, for a lab only.
- [ ] `name_prefix` globally unique; `address_space` distinct from every other
      instance you might peer.

## 3. Events reaching Event Hubs

- [ ] **Confirm Cribl can authenticate with Entra/OAuth.**
      `local_authentication_enabled = false` disables SAS entirely. Cribl's
      Event Hubs (Kafka-compatible) destination is commonly configured with a
      connection string — **that will not work here.** Verify before cutover;
      this is the most likely day-one surprise.
- [ ] Confirm the hub Cribl targets exists and is the one the processor consumes
      (§0).
- [ ] Watch **incoming messages** on the namespace. Zero here means the problem
      is upstream of pyre entirely.
- [ ] Partitions cap parallelism. 32 partitions = at most 32 concurrent
      consumers, regardless of Flex's instance ceiling.
- [ ] Confirm the `log_sender` identity actually holds **Azure Event Hubs Data
      Sender** on the namespace.

## 4. Redis — the highest-risk component

- [ ] **Check `maxmemory-policy`.** Azure defaults to `volatile-lru`, and
      **every** pyre key has a TTL, so all of them are eviction candidates.
      Under memory pressure Redis evicts:
      `seen:` → duplicate processing · `dd:` → thresholds reset ·
      `alert:` → **duplicate alerts**. All three are silent.
- [ ] **Size it against the real formula.** Resident keys ≈
      `events/sec × idempotency_ttl_seconds`. At 50k eps × 900s ≈ **45M keys
      (~4–5 GB)**. Pick an Azure Managed Redis SKU whose memory clears that with
      headroom — do **not** assume the `scale` default (`Balanced_B5`) is enough
      at high volume; check the SKU's advertised memory and size up.
      `idempotency_ttl_seconds` is the single dominant Redis cost: it is the only
      key written per *event* rather than per *match*.
- [ ] **Alarm on `evictedkeys > 0`.** Non-zero means dedup correctness is
      already degraded.
- [ ] **No Redis-version assumption to verify.** The dedup/threshold/storm
      windows use version-agnostic Lua — they set `EXPIRE` only when `TTL < 0`,
      never the Redis-7.0-only `EXPIRE key ttl NX` — so they run identically on
      the sim's Redis 6.0 floor and on Azure Managed Redis (Redis 7.x). fakeredis
      would accept `EXPIRE NX` and hide a 6.0-incompatibility, which is why
      `tools/sim` pins a real 6.0 server as the conservative floor.
- [ ] **Verify the processor MI has the Redis `Data Contributor` access
      policy.** The sim injects a Redis client, so the **TLS + Entra auth path is
      the one thing it does not cover** — the first real Redis call in Azure is
      where a problem would surface.

## 5. Alert delivery

- [ ] **Every destination you route to is `enabled: true`** in
      `config/destinations.yaml`. With `enabled: false` the Dispatcher never
      registers the route, so **every alert to it fails to deliver**, re-opens
      its dedup window, re-fires on the next match, and fails again — forever.
      `torq_prod` shipped `enabled: false`; it is fixed, keep it that way. See §1
      for why this is not a lab risk (a lab has no URL/token for it anyway).
- [ ] **Routing is the Terraform `default_routes` variable, per instance** — not
      any per-env config file. (The old dead `config/envs/*.yaml` have been
      removed; nothing read them.)
- [ ] Destinations are read **at startup**, not hot-reloaded like detections.
      Changing `destinations.yaml` requires a **restart or redeploy** of the
      Function App. (Detections hot-reload in ~45s; destinations do not.)
- [ ] Confirm the prod instance declares its destination in `destinations` **and**
      lists it in `default_routes`. An empty `default_routes` means any alert
      whose detection names no destination is undelivered — the engine warns once
      per worker at cold start (`DEFAULT_ROUTES is empty`); alarm on it (§7).
- [ ] Rehearse it before you trust it — this is exactly what caught
      `enabled: false`:
      `docker compose -f tools/sim/docker-compose.yml run --rm sim python tools/sim/run_pipeline.py --env prod`

## 6. Secrets and data leaks

- [ ] **The signals POST is unauthenticated.** `signals.py` posts the **full raw
      event body** to `signals_sink_url` with no `Authorization` header. That URL
      is therefore a bearer secret — and it is a plain Terraform variable, so it
      lands in **tfstate and app settings in cleartext**. If your Cribl HTTP
      source uses a token-in-URL, move it to Key Vault and confirm the endpoint
      is not internet-reachable. (This is in tension with
      [`docs/security.md`](../docs/security.md) §3.)
- [ ] Confirm Key Vault references resolved: the portal shows a green **Key Vault
      Reference** badge, not the literal `@Microsoft.KeyVault(...)` string.
- [ ] Restrict **Reader** on the Function App — anyone holding it can read every
      non-KV app setting value.
- [ ] The DaC PAT never reaches argv or disk (it rides an env-injected
      `http.extraheader`). Verified by
      `tools/sim::test_pull_does_not_leave_the_token_on_disk`.
- [ ] Terraform state is sensitive — private storage account, locked, access
      restricted.

## 7. Are events actually being evaluated?

- [ ] `./cli/pyre validate` passes. With a `default: true` hub in
      `sources.yaml`, a log type with no dedicated hub is **no longer an error**
      — it routes to the catch-all and is evaluated the same, so validate reports
      it as a summary line instead (*"866 detection(s) across 97 log type(s)
      have no dedicated hub"*). That took the panther-analysis fork from **1045
      errors to 1**, and the 1 that remains is real. Without a default hub the
      check reverts to erroring, because then those log types genuinely have
      nowhere to land.
- [ ] **Consider narrowing the bundle anyway — it's a cost lever.** `dac.include`
      / `dac.exclude` in `config/detections.yaml` now actually filter the pull
      (they were parsed and ignored before). The bundle is what every worker
      downloads and what the Registry YAML-parses on **each reload**, so a
      1045-file bundle is parsed in full every refresh even though a worker only
      ever *imports* the modules its own traffic can match. Narrowing `dac.path`
      or excluding subtrees you don't ingest cuts that directly.
- [ ] `./cli/pyre deps` ran in CI on **Python 3.11** (matching
      `runtime_version`). On any other version **the gate is lying** — it
      resolves imports against its own interpreter.
- [ ] Confirm `log_type_field` matches what Cribl actually stamps (`dataset` by
      default, not Panther's `p_log_type`).
- [ ] **Alarm on these — they are the silent-drop canaries**, not just log lines:

```kql
traces
| where timestamp > ago(1h)
| where message has_any (
    "batch dropped",                    // malformed JSON / missing log-type field
    "missing the 'log type' field",
    "matches no enabled detection",     // how a log-type rename kills coverage
    "skipping detection",               // a detection that won't import = coverage hole
    "failed to deliver",                // alerts not reaching Torq
    "storm limit hit",                  // alerts dropped by the limiter
    "SIGNALS_SINK_URL is not set",      // the entire audit trail is being discarded
    "DEFAULT_ROUTES is empty")          // alerts have nowhere to go by default
| order by timestamp desc
```

## 8. Deploy mechanics

- [ ] **`func azure functionapp publish` will fail from a laptop.**
      `public_network_access_enabled = false` + VNet integration means no public
      inbound. You need a self-hosted agent inside the VNet, or a temporary
      narrow firewall exception that you close again.
- [ ] Each of the `worker_process_count` (default 4) processes holds **its own
      bundle copy and its own Redis pool** inside `instance_memory_in_mb`
      (default 2048) — ~512 MB each. Check that against a real 900-detection
      bundle.
- [ ] After a DaC push, confirm it goes live within `refresh_interval_seconds`
      (~45s) with **no redeploy**.

## 9. First-hour smoke test

- [ ] Send one known-matching event → confirm **all three**: a signal in the
      lake, an alert record, and a Torq case. Not just the case.
- [ ] Send the same event twice with the **same partition + sequence number** →
      confirm exactly **one** signal (idempotency against real Redis).
- [ ] Send a non-matching event → confirm it produces nothing.
- [ ] Push a trivial DaC change → confirm live in ~45s.
- [ ] Check `evictedkeys`, `dispatch_failed`, and the §7 query are all clean.

---

## What CI now enforces

Everything above is only worth as much as the thing that runs it unprompted, so
it is wired into both `.github/workflows/ci.yml` and `.azure-pipelines/ci.yml`:

| Job | What it catches |
|---|---|
| `pyre pull` → `validate` → `deps` → `build` → `test` | a bundle that can't load, a log type with no home, an import the engine lacks |
| `terraform test` (14 checks, mock provider, offline) | greenfield plan regressions, SKU/cost drift, a publicly-reachable resource, shared-key auth, an inlined secret, a trigger bound to a hub that doesn't exist, a hub with no consumer |
| `tools/sim` (49 tests, real Redis 6.0) | dedup/idempotency/concurrency against a conservative Redis 6.0 floor (Managed Redis is 7.x, which accepts more), and the prod routing branch |

**The `detections` job had never passed once.** It ran `validate` without ever
running `pull`, against a gitignored `.bundle/` — so it exited 1 every run, and
none of the defects on this list were ever surfaced by it. (The ADO copy was
worse: it installed only `pyyaml pytest`, so `pyre test` could not have run at
all.) A gate nobody notices is red is the same as no gate.

---

## Known accepted tradeoffs (not bugs)

- **Duplicates over loss, by design.** Event Hubs is at-least-once and neither
  Redis nor Cribl can enlist in a transaction with it, so exactly-once is not
  available. A redelivery may duplicate signals and fire a threshold slightly
  early; alerts do **not** duplicate (the alert marker survives the retry).
- **A signals-flush failure *after* a successful dispatch loses the alert
  record** (not the alert, and not the signal). The redelivery's `register_alert`
  returns False, so the record is never re-buffered. Pinned by
  `tools/sim::test_signals_flush_failure_after_dispatch_loses_the_alert_record`.
- **Managed Redis `Balanced_B0` (`cost_profile = "test"`)** is the smallest SKU,
  for a lab: a patch/failover event can drop dedup state and cause duplicate
  alerts. Fine for a lab,
  never for prod.
