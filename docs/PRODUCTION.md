# pyre — the complete guide (build, deploy, run, scale, debug)

This is the single master document for pyre. It is written to take someone from "I've never seen this" to "I can stand up production, monitor it, debug it, and explain it to leadership." Read it top to bottom once; use the table of contents to jump back.

If a term is unfamiliar, it's defined in the **[GLOSSARY](GLOSSARY.md)** — keep it open in a second tab.

## Contents

1. [Executive summary (for leadership)](#1-executive-summary)
2. [What pyre is and the mental model](#2-what-pyre-is)
3. [Architecture, component by component](#3-architecture)
4. [How a log flows through it (two worked examples)](#4-how-a-log-flows-through)
5. [Environments: local, dev, prod](#5-environments)
6. [Everything you must decide and configure](#6-configuration-reference)
7. [Prerequisites](#7-prerequisites)
8. [Spin up PART 1 — test locally for $0](#8-spin-up-local)
9. [Spin up PART 2 — provision a cloud environment (dev or prod)](#9-spin-up-cloud)
10. [Spin up PART 3 — deploy the engine and detections](#10-deploy-engine-detections)
11. [Spin up PART 4 — connect Cribl (in) and Torq (out)](#11-connect-cribl-torq)
12. [What it looks like in Azure](#12-what-it-looks-like-in-azure)
13. [Monitoring and observability](#13-monitoring)
14. [Debugging playbook](#14-debugging)
15. [Scaling and cost](#15-scaling-and-cost)
16. [Updating a running system](#16-updating)
17. [Spinning it down](#17-spinning-down)
18. [Security posture](#18-security-posture)
19. [Demo](#19-demo)

---

<a name="1-executive-summary"></a>
## 1. Executive summary (for leadership)

**What it does.** pyre is a real-time security detection engine. Security logs from across the company stream in; pyre runs a library of detection rules against every log and opens a case in our SOC tooling when something looks malicious. It is a purpose-built, in-house replacement for the detection/alerting parts of Panther (our current SIEM), running on Microsoft Azure.

**Why build it.** Three reasons: (1) **cost** — Panther's pricing scales with data volume; pyre runs on cheap, serverless Azure primitives and scales to zero when idle; (2) **control** — detections live in our own Git repository with our review process and tests, and we own the pipeline end to end; (3) **flexibility** — we can extend it (enrichment, scheduled queries, AI triage) on our terms.

**Is it viable?** Yes. The core — running detections at scale — is proven and cheap. The honest scope is that Panther also does normalization, enrichment, and a search UI; pyre delegates normalization to **Cribl** (which we already run) and case management to **Torq**, and leaves enrichment/AI/scheduled-queries as clearly-seamed future modules.

**Scale.** Designed for **millions of logs per hour across hundreds of detections**. It stays cheap because it processes logs in **batches** (one compute invocation handles hundreds of logs), runs only the relevant detections per log, and **scales to zero** when traffic stops.

**Security posture.** Nothing is exposed to the internet — every service is private, reached over a private network, authenticated by managed identity with no passwords stored anywhere. Secrets live in Azure Key Vault.

**Cost.** A production environment's standing cost is modest (the always-on pieces are Redis and Event Hubs); compute is pay-per-use. A cheap dev environment runs on the smallest SKUs. Detailed numbers in [§15](#15-scaling-and-cost).

---

<a name="2-what-pyre-is"></a>
## 2. What pyre is and the mental model

Panther does five jobs. pyre keeps the middle three and hands the ends to specialized tools:

| Job | Who does it in the pyre world |
|---|---|
| 1. Clean up / normalize raw logs | **Cribl** (not built here) |
| 2. Run detections against logs | **pyre** (this repo) |
| 3. Remember state (dedup, thresholds) | **pyre** + Azure Redis |
| 4. Open and route cases | **pyre** → **Torq** |
| 5. Search / investigate UI | Torq + Cribl search (not rebuilt) |

The single most important design choice: **detections are not in this repo.** They live in a separate Git repository (like Panther's `panther-analysis`). pyre *pulls* them in, packages them, and the running engine *hot-reloads* them within ~45 seconds of a change. Security engineers edit detections in one place; pyre is the engine that runs them. This repo is the **engine + the infrastructure + the pipeline** — not the content.

```
   Logs (Okta, AWS,           pyre (this repo)                    Analyst
   firewalls, Cloudflare)     ┌────────────────────────┐         gets a case
        │                     │ route by log type      │            ▲
        ▼                     │ run the detections     │            │
   ┌─────────┐  normalized  ┌─┤ dedup / threshold      │     ┌──────┴──────┐
   │  Cribl  │─────────────▶│ │ open + route a case    │────▶│    Torq     │
   └─────────┘   & routed   └─┤ write signals to lake  │     └─────────────┘
                             └────────────────────────┘
        detections pulled from ▲
        an external Git repo ───┘
```

---

<a name="3-architecture"></a>
## 3. Architecture, component by component

Every Azure resource, what it's for, and its name (with `pyre` as the `name_prefix`):

| Component | Azure resource | Name | Role |
|---|---|---|---|
| **Ingestion bus** | Event Hubs namespace + `logs-in` hub | `pyre-ehns` | The high-throughput pipe Cribl sends logs into. Delivers logs to the engine in **batches** (the key cost lever). Sized from `config/sources.yaml`. |
| **Compute (the engine)** | Function App (Flex Consumption) | `pyre-proc` | Runs the detection code. Triggered by Event Hub batches. Scales 0→1000 instances with load. Holds all detections in one app (not one app per detection). |
| **State store** | Azure Cache for Redis | `pyre-redis` | Remembers dedup counts, thresholds, `unique()` counts, and the storm limiter between logs. Azure Functions are stateless, so this external memory is essential. |
| **Secrets** | Key Vault | `pyre-kv` | Holds real secrets (Torq token, Cribl creds). The engine reads them by reference; nothing sensitive is in a file. |
| **Storage** | Storage account (3 containers) | `pyrestor` | `bundle` = the engine's own deploy package; `detections` = published detection bundles; `checkpoints` = Event Hub read positions. |
| **Identity** | User-assigned Managed Identity | `pyre-mi` | The one identity the engine uses to authenticate to Event Hub, Redis, Blob, and Key Vault — no passwords anywhere. |
| **Network** | VNet + 2 subnets + private DNS | `pyre-vnet` | The private network. The engine joins it; every service is reachable only inside it. |
| **Private endpoints** | 4 of them | `pyre-*-pe` | Make Event Hub, Redis, Blob, Key Vault reachable only privately (never the internet). |
| **Monitoring** | Log Analytics + App Insights | `pyre-law`, `pyre-ai` | Where the engine's logs, metrics, and traces land so you can see it working and debug it. |

The engine code (in [engine/](../engine/README.md)) is small and modular: `function_app.py` (the thin trigger) → `processor.py` (the loop) → `registry.py`/`dac.py` (load detections) → `dedup.py` (Redis state) → `signals.py`/`dispatch.py` (outputs). See [engine/README.md](../engine/README.md) for the file-by-file breakdown.

---

<a name="4-how-a-log-flows-through"></a>
## 4. How a log flows through it (two worked examples)

**A log that becomes an alert:**
1. Cribl normalizes an Okta log, stamps `p_log_type = Okta.SystemLog`, and sends it to `pyre-ehns` / `logs-in`.
2. Event Hubs delivers a **batch** of logs to `pyre-proc`. Azure spins up an instance if none is warm (a few-second cold start; invisible for background work).
3. The engine reads `p_log_type` and looks up which detections apply (Palo logs never run Okta rules).
4. It runs each detection's `rule(log)`. Say `OktaAdminRoleAssigned` returns `True`.
5. It **always writes a signal** (the audit record: "this detection matched this log").
6. It computes a **dedup string** (e.g. `okta:admin:jdoe`) and does an atomic increment in Redis with a 1-hour expiry.
7. The count crosses the **threshold** (default 1) for the first time → this is a **new alert**. It runs the detection's `title()`, `severity()`, etc. on this first event.
8. It **opens a case in Torq** and writes an **alert record** to the lake.
9. If a second matching Okta log with the same dedup string arrives 10 minutes later: it still gets a signal, but Redis shows the alert already exists → the event is **grouped** into the existing case, no new alert. Title/severity stay as the first event set them.

**A log that does not alert:** steps 1–3 identical; every detection's `rule()` returns `False`; nothing is written — no signal, no alert. The instance idles and scales back to zero.

You can watch this exact behavior locally with `python tools/testlab/run_local.py` ([§8](#8-spin-up-local)).

---

<a name="5-environments"></a>
## 5. Environments: local, dev, prod

There is **one** Terraform composition (`infra/`). You deploy it as many **instances** as you want; each instance is a `.tfvars` file plus its own state key. **dev and prod are just two instances** of the same code — dev is prod with cheaper values. (A third "environment," **local**, is your laptop — no Azure, for developing this repo, and the only one meant to be permanent for feature work.)

| | **local** | **dev instance** (`envs/dev.tfvars`) | **prod instance** (`envs/prod.tfvars`) |
|---|---|---|---|
| Where | your laptop | Azure | Azure |
| Purpose | develop/test engine + detection logic | integration/staging, first cloud validation | the real thing |
| `name_prefix` | n/a | `pyredev` | `pyreprod` |
| Cost | $0 | cheapest SKUs (`cost_profile = test`) | scaled (`cost_profile = scale`) |
| Network | none | fully private, `10.0.0.0/16` | fully private, `10.1.0.0/16` |
| Redis | fake (in-memory) | Basic C0 | Premium, clustered |
| Event Hubs | none | Standard, 1 TU | Standard, auto-inflating |
| Function App | not deployed (run in-process) | Flex, scale-to-zero | Flex, up to 1000 instances |
| State key | none | `dev.tfstate` | `prod.tfstate` |
| Reachable from laptop? | n/a | no (private) | no (private) |

Because it's literally the same composition, what you prove in dev is what runs in prod — the difference is values, not shape. Spin up another instance (per-team, per-region) by copying a `.tfvars`, changing `name_prefix`/`env`/CIDR, and using a new state key.

> **Feeding a private environment for a demo/test.** Because dev and prod are private, you cannot push test logs from your laptop. Two honest options: (a) run the logic locally with `run_local.py` (best for showing detection behavior), or (b) for a live cloud demo, temporarily allow your IP on the Event Hub, push samples, then remove the rule — see [§14 "I need to feed the cloud engine for a demo"](#14-debugging). In real operation, Cribl (which lives in our network) is the sender; no laptop access is needed.

---

<a name="6-configuration-reference"></a>
## 6. Everything you must decide and configure

There are exactly three places you configure pyre. Nothing else needs editing to run it.

### 6.1 `config/detections.yaml` — where detections come from
| Field | What to set |
|---|---|
| `dac.repo` | Your detections Git repo URL (your fork of panther-analysis, or your own). |
| `dac.ref` | Branch/tag/commit to run. Pin a tag/sha for prod reproducibility. |
| `dac.path` | The subfolder in that repo holding the detections. |
| `dac.global_helpers` | Sibling dirs of shared `.py` helpers detections import by name (Panther `AnalysisType: global`), e.g. `[global_helpers]`. `pyre pull` bundles them and the engine puts them on the import path. Add `data_models` etc. if your detections import from them. |
| `dac.token_env` | Name of the env var holding a Git token (private repos only). |
| `bundle.mode` | `local` for laptop/tests, `blob` for deployed engine. |
| `bundle.refresh_interval_seconds` | How fast a detection push goes live (default 45s). |

### 6.2 `config/sources.yaml` and `config/destinations.yaml`
- **sources** — each log source's LogTypes, its Event Hub, and partition count (higher = more parallelism for a noisy source). Terraform reads this to size Event Hubs.
- **destinations** — where alerts go (`torq_dev`, `torq_prod`, `mock`). Secrets via Key Vault references, never inline.

### 6.3 Terraform variables (`infra/envs/<instance>.tfvars`)
Copy `infra/envs/dev.tfvars.example` / `prod.tfvars.example` to `<instance>.tfvars` and fill in:

| Variable | Meaning | Required? |
|---|---|---|
| `name_prefix` | Prefix for every resource name (default `pyre`). | no (has default) |
| `location` | Azure region (e.g. `eastus2`). | **yes** |
| `resource_group_name` | The resource group to deploy into. | **yes** |
| `cost_profile` | `test` (cheap) or `scale` (prod). | has default per env |
| `owner` | Tag value for cost attribution. | no |
| `cribl_sender_principal_id` | Object ID of Cribl's identity, so it may send to Event Hubs. | for real ingest |
| `publisher_principal_id` | Object ID of the CI service principal that runs `pyre publish`. | for CI publishing |
| `signals_sink_url` | Cribl HTTP source URL for signal/alert write-back. | for lake write-back |
| `refresh_interval_seconds` | Detection hot-reload interval. | no (45) |
| `storm_limit` | Alerts/detection/hour before suppression. | no (1000) |
| `throughput_units_floor` / `max_throughput_units` | (prod) Event Hub scaling floor/ceiling. | no |

**`.tfvars` files contain environment specifics and are gitignored — never commit them.**

---

<a name="7-prerequisites"></a>
## 7. Prerequisites

Install once:
- **Azure CLI** (`az`) — `az login`, then `az account show` to confirm the subscription.
- **Terraform** ≥ 1.6.
- **Azure Functions Core Tools** (`func`).
- **Python 3.11**.
- **Git**.

Access you need:
- **Owner** or **Contributor + User Access Administrator** on the target subscription (Terraform creates role assignments).
- A resource group to deploy into (e.g. `rg-pyre-dev`, `rg-pyre-prod`).
- For real ingest: the **object ID** of Cribl's Azure identity. For CI publishing: a **service principal with OIDC** federated to your Git host.

---

<a name="8-spin-up-local"></a>
## 8. Spin up PART 1 — test locally for $0

Prove the engine and your understanding before spending a cent. Full detail in [local-dev.md](local-dev.md); the essentials:

```bash
python -m venv .venv && source .venv/bin/activate   # PowerShell: .venv\Scripts\Activate.ps1
pip install -r engine/requirements.txt fakeredis pytest
python -m pytest tests -q                # the engine + detection tests
python tools/testlab/run_local.py        # run the REAL engine on sample logs
```

`run_local.py` runs `engine/pyre_engine/processor.py` against sample logs with an in-memory Redis and a local sink, printing the signals and alerts it produces. Edit a `Threshold:` in `tests/fixtures/sample_dac/…` and re-run to see a detection change take effect — the local mirror of a `git push` in production.

---

<a name="9-spin-up-cloud"></a>
## 9. Spin up PART 2 — provision a cloud instance (dev or prod)

There is one composition; you pick an instance with a `.tfvars` file and a state key. The steps below use `<inst>` = `dev` or `prod` (or any instance name you invent).

### 9.1 Bootstrap remote Terraform state, once per subscription
Every instance keeps state in Azure (durable, shared, lockable), one state blob each. Create the shared state store once:
```bash
az group create -n rg-tfstate -l eastus2
az storage account create -n pyretfstate -g rg-tfstate -l eastus2 \
  --sku Standard_LRS --min-tls-version TLS1_2 --allow-blob-public-access false
az storage container create -n tfstate --account-name pyretfstate --auth-mode login
```
(`infra/backend.tf` points at these names — change `pyretfstate` if it's taken.)

### 9.2 Configure the instance
```bash
cp infra/envs/<inst>.tfvars.example infra/envs/<inst>.tfvars
# edit: name_prefix (globally unique!), location, resource_group_name.
# dev keeps cost_profile="test"; prod keeps "scale". Each instance gets its own CIDR.
```

### 9.3 Init + plan — read the plan, it *is* the architecture
```bash
# from the repo root. The state key is what separates this instance's state.
terraform -chdir=infra init  -backend-config="key=<inst>.tfstate"
terraform -chdir=infra plan  -var-file=envs/<inst>.tfvars
```
The plan lists ~25 resources: the VNet + subnets + DNS, the Managed Identity, Storage (3 containers), Key Vault, Redis (+ its access policy for the identity), Event Hubs (+ receive/send role assignments), the Flex Function App, Log Analytics + App Insights, and 4 private endpoints. Confirm every service shows `public_network_access_enabled = false`.

### 9.4 Apply
```bash
terraform -chdir=infra apply -var-file=envs/<inst>.tfvars   # ~10–20 min (private endpoints + DNS are slow)
```
Note the outputs (`terraform -chdir=infra output`) — function app name, Event Hub FQDN, storage account. **Watch cost** in the portal (Cost Management, scoped to the resource group).

> Shortcut: the Makefile wraps all of this — `make init ENV=<inst>` then `make apply ENV=<inst>`.

### 9.5 Grant the two external identities
- **Cribl (to send logs):** set `cribl_sender_principal_id` in tfvars and re-apply (grants Cribl's identity *Event Hubs Data Sender*).
- **CI publisher (to publish detections):** set `publisher_principal_id` and re-apply (grants it write on the `detections` container).

---

<a name="10-deploy-engine-detections"></a>
## 10. Spin up PART 3 — deploy the engine and detections

### 10.1 Deploy the engine code
```bash
cd engine && func azure functionapp publish <name_prefix>-proc --python
```
This uploads `engine/` (the processor) to the Function App. It reads its settings (Event Hub, Redis, Key Vault, Blob — all by managed identity) from the app settings Terraform already wrote.

### 10.2 Publish the detections
```bash
export BUNDLE_BLOB_ACCOUNT_URL="https://<name_prefix>stor.blob.core.windows.net"
python cli/pyre pull        # clone the external DaC repo -> ./.bundle
python cli/pyre validate
python cli/pyre build
python cli/pyre publish     # upload bundle to Blob + flip the pointer
```
The running engine hot-reloads within `refresh_interval_seconds`. **In production this is automated:** a push to the detections repo fires `.github/workflows/publish-detections.yml`, which runs exactly these steps. Wire that once and detection changes ship by `git push` — no manual publish. See [cli/README.md](../cli/README.md).

---

<a name="11-connect-cribl-torq"></a>
## 11. Spin up PART 4 — connect Cribl (in) and Torq (out)

**Cribl → Event Hubs (logs in).** In Cribl, add an Event Hubs (Kafka-compatible) destination pointing at `<name_prefix>-ehns.servicebus.windows.net`, hub `logs-in`, authenticating with the Cribl identity you granted in §9.5. Cribl must stamp `p_log_type`, `p_event_time`, and indicator fields during normalization (this is the workstream pyre delegates to Cribl — see `PANTHER_CONVERSION.md` Part 11). Confirm partition/routing per `config/sources.yaml`.

**pyre → Torq (cases out).** In `config/destinations.yaml`, enable `torq_prod`, set its `url_env`/`token_env`; store the Torq token in Key Vault (`az keyvault secret set --vault-name <name_prefix>-kv --name torq-prod-token --value <token>`); point the env's default route at `torq_prod`. Republish is not needed for engine code, but the engine reads destinations at startup, so restart the Function App or redeploy the engine to pick up a new destination.

**Signals/alerts back to the lake.** Set `signals_sink_url` (a Cribl HTTP source) so every signal and alert is searchable alongside raw logs — this is what makes future AI triage and investigation useful.

---

<a name="12-what-it-looks-like-in-azure"></a>
## 12. What it looks like in Azure

Open **portal.azure.com → Resource groups → `rg-pyre-<env>`**. You'll see the resources from §3. The ones you'll actually click:

- **`pyre-proc` (Function App)** — the engine. Tabs that matter:
  - *Overview* → execution count and status (is it running?).
  - *Functions* → the `detect` function.
  - *Monitor* / *Logs* → live invocation logs and failures.
  - *Configuration / Environment variables* → the app settings Terraform set (Event Hub, Redis, bundle, storm limit). This is where you confirm the engine is pointed at the right things.
  - *Scale out* → how many instances are running.
- **`pyre-ehns` (Event Hubs)** → *Overview* shows incoming/outgoing message charts — proof that logs are arriving and being consumed.
- **`pyre-redis`** → *Metrics* (used memory, ops/sec) — the dedup state store's health.
- **`pyre-kv`** → *Secrets* — the Torq token etc. (values hidden; access via identity).
- **`pyrestor` → Containers → `detections`** → the published bundle zips and `current.json` pointer. Proof of what detections are live.
- **`pyre-law` (Log Analytics)** → *Logs* — where you run queries (next section).

Take screenshots of the Function App *Overview*, the Event Hubs incoming-messages chart, and the *Scale out* view — those three tell the "it's alive and it scales" story visually.

---

<a name="13-monitoring"></a>
## 13. Monitoring and observability

Everything the engine logs and measures flows to **Log Analytics (`pyre-law`)** via Application Insights. Go to `pyre-law` → **Logs** and run KQL.

**Is the engine running and healthy?**
```kusto
traces
| where timestamp > ago(1h)
| summarize count() by severityLevel, bin(timestamp, 5m)
| render timechart
```

**Failures / exceptions (detection errors, dependency issues):**
```kusto
exceptions
| where timestamp > ago(24h)
| summarize count() by type, outerMessage
| order by count_ desc
```

**Throughput (how many logs/batches processed):**
```kusto
requests
| where timestamp > ago(1h)
| summarize invocations=count(), avg(duration) by bin(timestamp, 5m)
| render timechart
```

**Alerts pyre generated** (the engine logs each dispatch): search `traces` for your dispatch log line, or query the alerts dataset in Cribl if `signals_sink_url` is wired.

**Alarms worth setting** (Azure Monitor alert rules on `pyre-law`):
- Function App exceptions spike → detection bug or dependency outage.
- Event Hubs *incoming messages = 0* for N minutes → a log source went silent (the equivalent of Panther's log-source-inactivity alarm).
- Redis *used memory* high or *server load* high → dedup store under pressure; scale it up.
- Poison/dead-letter growth on the Event Hub consumer → logs the engine couldn't process.

Metrics live on each resource's **Metrics** blade; logs and traces in **Log Analytics**; distributed traces and dependency maps in **Application Insights** (`pyre-ai`).

---

<a name="14-debugging"></a>
## 14. Debugging playbook

| Symptom | Where to look | Likely cause / fix |
|---|---|---|
| Engine deployed but no logs processed | Event Hubs `pyre-ehns` → incoming messages chart | Cribl isn't sending, or lacks the Data Sender role (§9.5), or isn't stamping the routing property. |
| Logs arriving, engine not triggering | Function App → Monitor; Event Hubs consumer lag | Engine not deployed, or its `EVENTHUB_CONNECTION` identity lacks *Data Receiver*. Check app settings. |
| Matches happen but no dedup / errors about Redis | App Insights `exceptions` (Redis auth/timeouts) | The Redis **access policy** for the identity is missing or the private endpoint/DNS isn't resolving. Confirm `azurerm_redis_cache_access_policy_assignment` applied. |
| `rule()` throwing for a detection | `exceptions` filtered by the detection id | A bug in that detection — it's isolated (one detection erroring doesn't stop the others). Fix in the DaC repo, republish. |
| Detection change not taking effect | `pyrestor` → `detections` container → `current.json` version | `pyre publish` didn't run/succeed, or you're within `refresh_interval_seconds`. Re-run publish; check the version bumped. |
| Alerts not reaching Torq | `traces` for dispatch errors; Key Vault access | Destination disabled/misconfigured in `config/destinations.yaml`, or the token secret/Key Vault reference is wrong. |
| Alert storm | `traces` for storm-limiter messages | A bad detection; the storm limiter caps it at `storm_limit`/hour and keeps signals. Tune or disable the detection in the DaC repo. |
| `terraform apply` fails on a SKU/region | the apply error | Free-trial quota or region availability — change region or SKU. |

**"I need to feed the cloud engine for a demo" (private env, no laptop access):**
```bash
# 1) temporarily allow your IP on Event Hubs
MY_IP=$(curl -s ifconfig.me)
az eventhubs namespace network-rule-set ip-rule add \
  --namespace-name <name_prefix>-ehns -g rg-pyre-<env> --ip-address $MY_IP --action Allow
az eventhubs namespace update -n <name_prefix>-ehns -g rg-pyre-<env> --public-network-access Enabled
# 2) push samples
python tools/testlab/python_shipper.py \
  --namespace <name_prefix>-ehns.servicebus.windows.net --hub logs-in \
  --file tools/testlab/samples/palo_sample.jsonl --rate 50
# 3) REMOVE the exception afterward
az eventhubs namespace update -n <name_prefix>-ehns -g rg-pyre-<env> --public-network-access Disabled
```
This keeps the environment private by default and opens it only for the length of a demo, then re-seals it.

---

<a name="15-scaling-and-cost"></a>
## 15. Scaling and cost

**How it scales to millions/hour.** Three multipliers:
1. **Batching** — Event Hubs hands the engine hundreds of logs per invocation (`host.json` `maxEventBatchSize = 256`). Compute cost ≈ total logs ÷ batch size, so a 256-batch is ~256× cheaper than one-log-at-a-time.
2. **Parallelism** — Flex Consumption adds instances as the Event Hub backlog grows, up to 1000 in prod; Event Hub **partitions** (from `config/sources.yaml`) set the parallelism ceiling per source. Give a noisy source its own hub with more partitions.
3. **Per-log-type routing** — each log only runs its relevant detections, so adding the 400th detection doesn't slow the 399 others.

And it **scales to zero**: when logs stop, instances drain and you pay nothing for compute.

### Latency & batching (does batching slow alerts? No.)

A common worry: "batching means we wait to fill a batch, so alerts are slow." That's not how Event Hubs works. **`maxEventBatchSize` is a ceiling, not a wait.** The trigger delivers whatever events are already in the partition, immediately, up to that cap:
- Light traffic (3 logs waiting) → a batch of 3 is delivered **now** and processed in milliseconds. A high ceiling adds zero delay.
- Heavy traffic → batches of up to 256 let the engine drain a backlog cheaply and fast.

This is exactly how Panther works — **micro-batch streaming** on Lambda/Fargate, near-real-time (seconds), not per-event-synchronous. Every high-volume SIEM micro-batches; true per-event processing at millions/hour isn't viable anywhere. So batching is the correct, industry-standard model, and it does **not** trade away alert speed.

**What actually drives alert latency (tune these):**
1. **Cold starts** — the #1 source. Scaling from zero adds a few seconds. Fix: run **always-ready (warm) instances** in prod (Flex Consumption's always-ready feature; set via the portal/CLI on the Function App). Biggest speed win.
2. **Cribl's egress flush interval** — Cribl batches before sending to Event Hubs; often the largest single contributor. Lower it for latency-critical sources.
3. Checkpoint frequency / prefetch (`host.json`) — minor.
4. Batch size — essentially irrelevant to latency (it's a ceiling).

**Batch ↔ single-event is a per-instance knob.** Set `max_event_batch_size` in the instance's `.tfvars`:
- `256` (default) — batched; the scalable, cost-efficient choice. Recommended even for latency-critical prod, because it doesn't delay alerts.
- `1` — single-event; one invocation per log. Only for a **low-volume, latency-insensitive-to-cost** instance, since compute cost becomes ≈ log count and you lose Redis pipelining. It does **not** make a lone alert meaningfully faster than a small batch would.

It works by overriding `host.json` at runtime via the app setting `AzureFunctionsJobHost__extensions__eventHubs__maxEventBatchSize` — so you change it in `.tfvars` + `terraform apply`, no code change and no redeploy of the engine. The processor handles any batch size (1..N) unchanged.

**The Panther "Lambda vs Fargate" analogue.** For a source that is high-volume 24/7, always-on containers can beat per-execution billing. The same engine package runs on **Azure Container Apps** unchanged (only the entry point differs). Start everything on Flex; move a single sustained-high-volume source to Container Apps only if the math says so.

**Cost shape (approximate, region-dependent — verify with the Azure Pricing Calculator):**
- *dev* (`test`): Redis Basic C0 (~$16/mo) + Event Hubs 1 TU (~$11–22/mo) + 4 private endpoints (~$29/mo) + small Log Analytics ≈ **$60–80/mo if left on**; destroy when idle to pay ~nothing.
- *prod* (`scale`): Premium Redis + auto-inflating Event Hubs + always-warm capacity — sized to your real volume; compute is pay-per-use and dominated by log volume, not idle time.

The levers to tune cost/log: `maxEventBatchSize`, partition counts, Redis tier, and moving a firehose source to Container Apps. Measure with the cost breakdown by tag (every resource is tagged `system`/`env`/`owner`).

---

<a name="16-updating"></a>
## 16. Updating a running system

Cheapest-first:
- **A detection** (new/edit/tune) → push to the DaC repo; CI (or `pyre pull && build && publish`) republishes; engine hot-reloads in ~45s. **No redeploy.**
- **Engine code** → `cd engine && func azure functionapp publish <name_prefix>-proc --python`.
- **A destination or source** → edit `config/*.yaml`; re-`apply` (sources change Event Hub sizing) and/or restart the engine (destinations read at startup).
- **Infra** (SKU, setting) → edit the module or `envs/<inst>.tfvars`, then `terraform -chdir=infra apply -var-file=envs/<inst>.tfvars` (or `make apply ENV=<inst>`). Terraform changes only what differs.

Detection changes are the 95% case and are the fast, no-redeploy path — that's the whole point of the external-DaC design.

---

<a name="17-spinning-down"></a>
## 17. Spinning it down

```bash
terraform -chdir=infra destroy -var-file=envs/<inst>.tfvars    # or: make destroy ENV=<inst>
```
Deletes everything that instance created (its state key isolates it — destroying dev never touches prod). Because detections and config live in Git, nothing of value is lost — re-run §9–§10 to bring it back. For **dev**, do this whenever you finish a test session (protects a trial budget). For **prod**, destroy is a deliberate decommission, not a daily action.

To confirm: portal → the resource group should be empty afterward. (Deleting the resource group directly also removes everything, but leaves Terraform state stale — only do that if you also delete the state.)

---

<a name="18-security-posture"></a>
## 18. Security posture (for review / compliance)

- **No public exposure.** Every service has `public_network_access_enabled = false` and is reached only over a private endpoint inside the VNet. The Function App has no public inbound; it pulls from Event Hubs.
- **No secrets in code or config.** All service-to-service auth uses a **Managed Identity** (Entra), never keys — Event Hubs and Storage have local/shared-key auth disabled. Real secrets (Torq token) live in **Key Vault** and are read by reference.
- **Least privilege.** The engine identity holds only the specific data roles it needs (Event Hubs Receiver, Redis Data Contributor, Blob Data Contributor, Key Vault Secrets User). The CI publisher is scoped to just the `detections` container.
- **Auditability.** Infrastructure is Terraform (version-controlled, reviewable). Detections are Git with review + tests. Deploys go through CI. Everything the engine does is logged to Log Analytics.
- **Data handling.** Signals/alerts are written back to the Cribl lake with per-dataset retention you control.

---

<a name="19-demo"></a>
## 19. Demo (a suggested 15-minute script)

1. **The problem & the bet** (2 min) — §1. "We pay Panther by data volume. This does the detection/alerting in-house on cheap serverless Azure, keeps detections in our Git with our reviews, and delegates the parts we shouldn't rebuild (normalization → Cribl, cases → Torq)."
2. **Show it working, no cloud needed** (3 min) — run `python tools/testlab/run_local.py` live. Point at the output: "two malicious logs matched → two signals → deduplicated into one case; a below-threshold match logged a signal but correctly did *not* page anyone." This is the real engine.
3. **Show a detection change** (2 min) — edit a threshold, re-run: "a detection change is a Git push; in production the running engine hot-reloads it in under a minute, no redeploy."
4. **Show the architecture** (3 min) — the diagram in §2 and the resource-group view in Azure (§12). "Nothing is on the internet; everything authenticates by identity with no stored passwords."
5. **Show it scales** (3 min) — §15: batching + scale-out + scale-to-zero, and the Event Hubs incoming-messages / Function App scale-out charts. "Designed for millions of logs/hour; costs nothing when idle."
6. **Cost & status** (2 min) — §15 numbers and honest status: core is proven; normalization/enrichment/AI-triage/scheduled-queries are seamed future work with clear scoping in `PANTHER_CONVERSION.md`.

Leave-behind: this document, plus `docs/architecture.md` (one-pager) and `GLOSSARY.md`.

---

*Related docs: [architecture.md](architecture.md) (the one-page why), [local-dev.md](local-dev.md) (the $0 local workflow), [security.md](security.md), [GLOSSARY.md](GLOSSARY.md), and each directory's own `README.md`.*
