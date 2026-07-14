# Glossary — every term in this repo, in plain English

Written for someone brand new to security operations, Azure, and Terraform. Skim it once; refer back whenever a word in another doc is unfamiliar. Terms are grouped by topic, not alphabetized, so related ideas sit together.

---

## The problem we're solving

**Log** — a single record that something happened: "user jdoe logged in from 1.2.3.4 at 10:00," "firewall allowed a connection to port 3389." Systems produce millions of these. Most are boring; a few indicate an attack.

**SIEM** (Security Information and Event Management) — the category of product that collects logs and alerts on suspicious ones. pyre is a home-built piece of a SIEM.

**Panther** — the commercial SIEM this project replaces. We copy its *behavior* (how it runs detections, dedups, alerts) but rebuild it on Azure. When a doc says "Panther does X," that's the behavior we're matching.

**Detection** (also **rule**) — a small piece of code that looks at one log and returns true/false: "is this suspicious?" Example: "a firewall *allow* to port 3389 (Remote Desktop) is worth flagging." Detections are the valuable content; pyre is the engine that runs them.

**Detection-as-Code (DaC)** — the practice of keeping detections as normal code files in a Git repo (so they get reviews, version history, and tests) instead of clicking around in a web UI. pyre reads detections from an **external** DaC repo.

**panther-analysis** — Panther's public example DaC repo (github.com/panther-labs/panther-analysis). A model for what your detections repo looks like: paired `.py` (logic) + `.yml` (settings) files.

**Signal vs. Alert vs. Case** — three escalating things:
- **Signal** — recorded *every time* a detection matches. Pure audit trail: "detection X matched log Y." Cheap and complete.
- **Alert** — created only when matches cross a threshold and aren't duplicates (see dedup). This is "a human should probably look."
- **Case** — the alert delivered into a tool where an analyst works it (assign, comment, resolve). In pyre, cases live in **Torq**.

**Dedup (deduplication)** — grouping repeats so you get *one* alert, not a thousand. If the same user trips the same detection 500 times in an hour, that's one alert with 500 events attached, not 500 alerts. pyre computes a **dedup string** (e.g. `user:jdoe`) per match and collapses matches that share it within a time window.

**Threshold** — how many matches (sharing a dedup string) must happen before an alert fires. Default 1 (alert on the first). A brute-force detection might set 5 ("only alert after 5 failed logins").

**unique()** — a threshold on *distinct* values rather than total count: "alert if 5+ **different** source IPs hit this account," not "5 events."

**Dedup window / period** — the time span over which dedup and thresholds apply, after which the count resets. Default 1 hour.

**First-event-wins** — once an alert exists, later matches attach to it but don't change its title/severity. The first matching event sets those. pyre reproduces this.

**Storm limiter** — a safety cap: if one detection somehow fires 1000+ alerts in an hour (a bad rule), pyre stops sending more to protect the case tool, but keeps recording signals so nothing is lost.

**Enrichment** — extra context attached to a log before detections run, e.g. "this IP is from Russia" or "this account is a known admin." Optional in pyre v1.

**Runbook** — written instructions for what to do when a detection fires ("check X, then Y"). Stored alongside the detection.

---

## Where logs come from

**Cribl** — a separate product (not built here) that receives raw logs, cleans/normalizes them, and forwards them. It does the un-glamorous prep work so pyre receives tidy logs. Think of Cribl as the mailroom and pyre as the analyst.

**Normalization** — reshaping messy vendor-specific logs into a consistent format with standard fields, so a detection can rely on the fields being there.

**p_ fields** — the standard fields normalization adds, borrowed from Panther's naming: `p_log_type` (what kind of log — `Palo.Traffic`, `Okta.SystemLog`), `p_event_time` (when it happened), etc. pyre routes and processes based on these.

**Log type / LogType** — the label for a kind of log (e.g. `Cloudflare.HttpRequest`). Every detection declares which log types it applies to; pyre uses that to run only the relevant detections per log — a Palo log never runs Cloudflare rules.

---

## Azure — the cloud we run on

**Azure** — Microsoft's cloud. You rent computing and services instead of owning servers.

**Resource group** — a folder that holds related Azure resources (yours is `rg-pyre-dev`). Delete the group and everything in it goes — handy for a clean teardown.

**Azure Function** — a small piece of code Azure runs *on demand* when something triggers it. You don't manage a server; Azure runs your code when needed and charges per run. pyre's detection engine is an Azure Function.

**Function App** — the container that holds one or more Functions and their settings. pyre puts *all* detections inside *one* Function App (not one app per detection) — the same pattern Panther uses.

**Trigger** — what causes a Function to run. pyre's engine uses an **Event Hub trigger** (runs when logs arrive).

**Event Hub** — Azure's high-throughput pipe for streaming data — built for millions of events/hour, cheap per event, and it hands your Function a *batch* of logs at once (the key to keeping cost low). Cribl sends logs here.

**Batch** — many logs delivered/processed together in one Function run. Cost ≈ number of logs ÷ batch size, so bigger batches = cheaper.

**Flex Consumption** — the specific Azure Functions hosting plan pyre uses. "Serverless": scales to **zero** when no logs arrive (you pay nothing idle) and scales out to many copies under load.

**Cold start** — the couple-seconds delay the first time a scaled-to-zero Function wakes up. Invisible for background log processing.

**Scale to zero / scale out** — automatically dropping to no running copies when idle, and adding copies when busy. Why serverless is cheap for bursty traffic.

**Container Apps** — an alternative always-on hosting option for a source that's high-volume 24/7 (documented as a future path, not used yet).

---

## Remembering things & staying secure

**Redis** (Azure Cache for Redis) — a very fast in-memory database pyre uses to *remember* things between logs: dedup counts, thresholds, the storm limiter. Azure Functions forget everything between runs, so this external memory is essential. Its speed (sub-millisecond) keeps per-log cost tiny.

**TTL (time-to-live)** — an automatic expiry on a stored value. pyre implements the dedup *window* simply by giving a Redis key a TTL equal to the window; it vanishes on its own when time's up.

**Managed Identity (MI)** — an Azure identity for a resource (like the Function App) that lets it authenticate to other Azure services **without any password**. Azure vouches for it. pyre uses one MI for all its service-to-service auth — no secrets in code.

**Entra / Entra ID** — Microsoft's identity system (formerly Azure Active Directory). Managed Identities live here.

**RBAC (Role-Based Access Control)** — granting an identity a specific role on a specific resource (e.g. "this MI may *read* blobs in that container"). Least privilege: each identity gets only what it needs.

**Key Vault** — Azure's secrets safe. pyre deploys **two per instance**: the engine's own vault (`<name_prefix>-kv`, real secrets like the Torq API token, read by the Function App at runtime by identity) and a separate CI-only vault (`<name_prefix>-ci-kv`, only for secrets the publish pipeline needs, e.g. a cross-org DaC PAT). Two vaults so a compromised identity on one side can't read the other's secrets. Nothing sensitive sits in a config file either way.

**Private endpoint** — a setting that makes an Azure service reachable *only* from inside a private network, never the public internet. Maximally secure, but it means your laptop can't reach it, and each one costs ~$7/month. pyre uses them everywhere in both dev and prod — nothing is on the internet.

**VNet (Virtual Network)** — a private network inside Azure. The Function App joins the VNet so it can reach the private services; every service lives behind a private endpoint in it.

**public_network_access_enabled** — a per-service switch. pyre sets it `false` everywhere (private only). To feed a private environment for a one-off demo, you temporarily add your IP to a service's firewall and remove it afterward (see PRODUCTION.md § Debugging) — the default stays private.

---

## How detections get from the repo into the running engine

**Bundle** — a folder (or zip) containing the detection files, packaged for the engine to load. pyre builds a bundle from the external DaC repo.

**pull** — `pyre pull` clones the external detections repo at a pinned version into a local bundle.

**publish** — `pyre publish` uploads that bundle to Azure Blob storage and updates a small pointer file, so the running engine can pick it up.

**Blob / Blob Storage** — Azure's file/object storage. pyre stores published detection bundles here.

**Hot-reload** — the running engine notices (within ~45 seconds) that a new bundle was published and swaps in the new detections **without a redeploy or restart**. This is how a `git push` to the detections repo goes live fast, even at scale.

**Registry** — the engine's in-memory index of "log type → which detections to run," built from the bundle. It's what makes running hundreds of detections cheap: each log only touches its relevant few.

**App Configuration** — an Azure service for feature-flag-style settings (planned use: turning individual detections on/off live). Currently a stub in pyre.

---

## Delivering alerts

**Destination** — where an alert is sent. pyre supports `mock` (a test sink that just logs), `webhook` (generic), and `torq`.

**Torq** — the security automation / case-management tool that receives pyre's real alerts and where analysts work them.

**Dispatch** — the act of sending an alert to its destination(s).

**Mock destination** — a tiny fake destination (`tools/mocks/`) used in testing; it just records whatever alert it receives so you can watch the pipeline work.

---

## Building the cloud environment

**Terraform** — a tool that creates cloud resources from code. You describe what you want (a Function App, a Redis, etc.) in `.tf` files; Terraform makes Azure match. Change the file, re-run, and it updates only what changed.

**HCL** — the language Terraform files are written in.

**Module** — a reusable Terraform building block for one concern (pyre has a module each for network, identity, storage, redis, eventhub, keyvault, function_app, monitoring). The env "composition" wires modules together.

**Composition** — the top-level Terraform (`infra/main.tf`) that instantiates and connects the modules. There's one composition; each **instance** (dev, prod, …) is that composition deployed with a different `.tfvars`.

**State** — Terraform's record of what it has created, so it knows what to change next time. Kept in a local `terraform.tfstate` file (fine for a solo lab) or a shared remote location (for teams).

**plan / apply / destroy** — Terraform's three verbs. `plan` = preview changes, `apply` = make them, `destroy` = delete everything it created (your off switch to stop spending).

**tfvars** — a file of values you plug into the Terraform variables (region, resource group, `cost_profile`, identity IDs). Copy `dev.tfvars.example` / `prod.tfvars.example` to `<env>.tfvars` and fill it in. Gitignored — never committed.

**instance** — one deployment of pyre's single Terraform composition, selected by a `.tfvars` file (its `name_prefix`, region, `cost_profile`, CIDR) and its own state key. There is one code base; you stamp out as many instances as you want. This is how real products manage environments.

**dev / prod** — the two standard instances (`infra/envs/dev.tfvars`, `infra/envs/prod.tfvars`). Same composition, same private posture; **dev is just prod with cheaper values** — `cost_profile = test` vs `scale`, a different `name_prefix` and CIDR, and its own state key. Both use remote state. (A third "environment," **local**, is your laptop — no Azure, for developing this repo.)

**cost_profile** — a pyre knob: `test` (cheapest SKUs, the floor on cost) or `scale` (bigger, for real production volume).

---

## Tools you'll type

**Azure CLI (`az`)** — the command-line tool for Azure. `az login` signs you in.

**Azure Functions Core Tools (`func`)** — the command-line tool for deploying/running Functions.

**pyre CLI (`pyre`)** — this project's own command-line tool (`cli/pyre`). Wraps the detection workflow: `pull`, `validate`, `build`, `publish`, `deploy`, `enable`, `disable`.

**pytest** — the Python test runner. `pytest` runs everything in `tests/`.

**fakeredis** — a pretend, in-memory Redis used only for local testing, so you don't need a real Redis to run the engine on your laptop.

**OIDC / Workload Identity Federation** — a way for automation (an Azure Pipelines run, in this repo) to authenticate to Azure without storing a password, using short-lived federated tokens. The Azure DevOps service connection type is "Azure Resource Manager (Workload identity federation)". Used by the publish pipeline.

**PAT (Personal Access Token)** — a Git access token, used only to clone a *private* detections repo. It never touches the running engine.
