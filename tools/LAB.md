# The pyre lab: see the whole system work, in Azure, on your own subscription

For: **your own Azure subscription**, **no Cribl**, **no Torq** — a mock sink is
the destination — and **your machine's network logs** as the only real feed. The
**local** parts (0–4) run on your laptop for $0; the **cloud** parts (5+) run from
a small **deployer VM inside the VNet**, because everything is private (no public
endpoints, no IP allowlist) — exactly the company posture.

You will end up with: your network connections streaming into Event Hubs → the
real engine evaluating them → a detection you wrote firing → an alert landing in
a mock destination you can read. Then you'll change the detection and watch it go
live in ~45 seconds with no redeploy.

> ### Read this first
>
> **Commands are PowerShell** (Windows). Run them in a PowerShell terminal; a few
> steps (network capture) want an **elevated** one — "Run as Administrator".
>
> **You are paying for this.** The standing cost is Redis + Event Hubs; compute
> scales to zero. Redis is now the dominant line item: the classic Basic C0
> (~$16/mo) is **retired** and Azure refuses to create it, so this lab uses
> **Azure Managed Redis, Balanced_B0** — the smallest SKU, but billed by the hour
> and noticeably pricier than the old Basic cache (check the Managed Redis pricing
> page for your region; budget on the order of a few dollars for a same-day lab,
> and clearly more than the old ~$25–40/mo if you leave it up). Event Hubs
> Standard 1 TU is ~$11/mo, storage/Log Analytics a few $.
> **[Part 11](#11-tear-it-down) is not optional.** Do the lab in one sitting and
> destroy it — with Managed Redis billed hourly, teardown matters more than ever.
>
> **This deploys the real, secure architecture — no shortcuts.** Everything is
> private (private endpoints), with **no public endpoints and no IP allowlist** —
> the exact company posture. Entra/Managed Identity is the only way in anywhere —
> no SAS, no shared keys. Because nothing is reachable from the internet, the
> cloud steps run from a **deployer VM inside the VNet** (Part 5.0); RBAC governs
> that deployer's identity (`admin_principal_ids`), VNet membership its reachability.

**Parts 0–4 are free and run on your laptop.** Don't skip them — they're how you
know the engine works before you pay Azure a cent.

---

## 0. The mental model (2 minutes, saves you an hour)

| | |
|---|---|
| **pyre** (this repo) | the engine + infrastructure. Deploy once, rarely edit. |
| **[pyre-dac](https://github.com/xtrajank/pyre-dac)** | your detections. `config/detections.yaml` already points here, at ref `laptop-test`. **This is where you write rules.** |

Three facts that explain every step below:

1. **`dataset` decides everything.** The engine reads that field off each event
   to pick which detections run. No `dataset` → the event is dropped before any
   rule sees it. In production Cribl stamps it; here **you** stamp it, in
   `capture_netlogs.py`. That is the only thing you are simulating.
2. **`dataset` must be a value `config/sources.yaml` knows.** Use
   **`ICO.Network`** — already declared. Invent a name and `pyre validate`
   rejects your detection, because no Event Hub is sized for it.
3. **Detections live outside this repo and hot-reload.** Push to `pyre-dac`, run
   `pyre publish`, and every running worker picks it up in ~45s with no redeploy.
   That is the thing worth seeing.

```text
deployer (in VNet)         Event Hubs            the engine              mock sink
capture_netlogs.py  ──▶   pyredev1-cribl-ehns ─▶  pyredev1-proc      ──▶   pyredev1-mockdest
(stamps dataset)          default-logs-in       routes by dataset       you read the alert
python_shipper.py                               runs pyre-dac rules
(ships them, from VNet)                         dedups, alerts
```

---

## 1. Prerequisites

```powershell
python --version     # 3.11 ideally (matches the Function App); 3.12 is fine for the lab
git --version
az --version         # Azure CLI
terraform -version   # >= 1.7
func --version       # Azure Functions Core Tools v4
```

```powershell
git clone https://github.com/xtrajank/pyre.git; cd pyre
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r tests/requirements.txt psutil
```

> If `Activate.ps1` is blocked by execution policy, allow it for this session
> only: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned`.

## 2. Prove it locally, for $0

Do this before you touch Azure. Same engine code, fake Redis, local sink:

```powershell
python tools/testlab/run_local.py
```

```text
SIGNALS  written to lake (one per rule match): 2
ALERTS   (after threshold + dedup + storm-limit): 1
DISPATCHED to destination (mock): 1
```

**4 events → 2 matched → 1 alert.** Both matches shared a dedup string, so they
collapsed into one alert. That collapse *is* the dedup working.

Now check the infrastructure without deploying it — a real plan against a mock
provider: no subscription, no credentials, no cost:

```powershell
terraform -chdir=infra test        # must be 16/16
```

Optional but recommended — the high-fidelity run against a **real Redis 6.0**, a
deliberately conservative floor (Azure Managed Redis is Redis 7.x-compatible and
_accepts more_; fakeredis above accepts syntax a real server can reject, so this
catches what fakeredis won't):

```powershell
docker compose -f tools/sim/docker-compose.yml run --rm sim     # 49 tests
docker compose -f tools/sim/docker-compose.yml down -v
```

## 3. Capture your network logs

Run these in an **elevated PowerShell** ("Run as Administrator"), or
`process_name` degrades to `"unknown"`.

```powershell
python tools/testlab/capture_netlogs.py -o network_logs.jsonl
Start-Sleep 5
python tools/testlab/capture_netlogs.py -o network_logs.jsonl
Start-Sleep 5
python tools/testlab/capture_netlogs.py -o network_logs.jsonl
(Get-Content network_logs.jsonl | Measure-Object -Line).Lines
```

Check the shape:

```powershell
Get-Content network_logs.jsonl -TotalCount 1 | python -m json.tool
```

```json
{ "dataset": "ICO.Network", "_time": 1784156569.35, "remote_ip": "184.30.91.87",
  "remote_port": 443, "process_name": "chrome.exe", "hostname": "your-host" }
```

`dataset` and `_time` are stamped by the script. That is Cribl's whole job here,
and the only part of it you are standing in for.

## 4. Write your detection in `pyre-dac`

Port **4444** is a good first rule: nothing you do normally hits it, so you can
fire it on demand.

```powershell
cd ..; git clone https://github.com/xtrajank/pyre-dac.git; cd pyre-dac
git checkout laptop-test          # the ref config/detections.yaml points at
New-Item -ItemType Directory -Force rules/ico_network_rules/custom | Out-Null
```

`rules/ico_network_rules/custom/network_suspicious_port.py`:

```python
def rule(event):
    return event.get("remote_port") == 4444


def title(event):
    return f"Connection to suspicious port 4444 from {event.get('hostname', 'unknown')}"


def severity(event):
    return "Medium"


def alert_context(event):
    # What a responder needs to triage this without going to look it up.
    return {
        "remote_ip": event.get("remote_ip"),
        "process_name": event.get("process_name"),
        "pid": event.get("pid"),
    }
```

`rules/ico_network_rules/custom/network_suspicious_port.yml`:

```yaml
AnalysisType: rule
RuleID: ICO.Network.SuspiciousPort
Filename: network_suspicious_port.py
Enabled: true
LogTypes:
  - ICO.Network          # MUST match `dataset` on your logs AND config/sources.yaml
Severity: Medium
Threshold: 1             # alert on the first match
DedupPeriodMinutes: 15   # later matches in 15m group into the same alert
```

```powershell
git add rules/ico_network_rules/custom
git commit -m "Add suspicious-port network detection"
git push
```

**Prove it locally before deploying anything:**

```powershell
cd ../pyre
python cli/pyre pull                  # clones pyre-dac@laptop-test -> .bundle/
'{"dataset":"ICO.Network","_time":"2026-07-15T00:00:00Z","remote_ip":"203.0.113.9","remote_port":4444,"process_name":"nc","hostname":"test-host"}' | Add-Content network_logs.jsonl
python tools/testlab/run_local.py --bundle .bundle --file network_logs.jsonl
```

You should see **at least one** SIGNAL, ALERT and DISPATCH for
`ICO.Network.SuspiciousPort`. **If it doesn't fire here it won't fire in Azure** —
fix it now, while the loop is free and instant.

Two things you may notice, both correct:

- **More than one alert.** Your capture may already contain a real port-4444
  connection, on top of the synthetic one. This detection has no `dedup()`, so
  the dedup string falls back to `title()` — which includes the hostname — so a
  different host is a different alert. Add `def dedup(event): return "port-4444"`
  and they collapse into one.
- **`batch dropped 1/N event(s): 1 malformed json`.** A capture interrupted
  mid-write leaves a partial line, and the next run appended onto it. The engine
  counted it and processed everything else, which is the whole point: a dropped
  event is never silent. `capture_netlogs.py` now closes the gap before
  appending, so re-capture and it goes away.

> `python cli/pyre validate` currently reports **1 error**, on a Panther "Simple
> Detection" already in the DaC repo (`github_repo_archived.yml` — YAML-only
> logic, no Python). pyre runs Python rules only, so it would be skipped
> silently; validate refuses it instead. It does not block this lab.

---

## 5. Deploy the infrastructure

```powershell
az login
az account show          # confirm the RIGHT subscription — you are paying
az account set --subscription "<your-subscription-id>"
```

You need **Owner** — Terraform creates role assignments.

### 5.0 The deployer runs inside the VNet

Because nothing is public, every cloud step (terraform's storage data-plane
calls, `func publish`, `pyre publish`, shipping logs) runs from **inside the
VNet** — a laptop outside it has no network route to the private endpoints, no
matter its RBAC. Access is gated by identity: only a principal that can auth to
Azure with the right RBAC (`admin_principal_ids`) can change anything.

In the company this is settled infrastructure: your CI/CD pipeline runs on a
**VNet-resident agent** (self-hosted, or a peered management network), whose
identity is one of the admins. RBAC governs *who*; VNet membership governs
*reachability*. You add operators by adding them to the admin Entra group.

For a **solo laptop** with no in-VNet compute, the cloud parts (5+) need such an
in-VNet entry point (a jumpbox reached via Azure Bastion, or a peered network) —
this composition intentionally does **not** create one, so it never carries a
standing VM you'd forget to remove. The **local** parts (0–4) run fully on your
laptop for $0 and already exercise the whole engine.

### 5a. Start from a clean slate

If earlier attempts left half-built resources, wipe them so nothing collides
(skip if the RG doesn't exist):

```powershell
az group delete -n rg-pyre-dev --yes
```

### 5b. Verify Managed Redis provisions in your region — before the full build

The "prove a value is correct before betting on it" step. Managed Redis
`Balanced_B0` fails to place in some regions (West US 2 was one). Test it
standalone first — ~2 min, pennies:

```powershell
az extension add -n redisenterprise --upgrade
az group create -n rg-pyre-redistest -l westus2
az redisenterprise create -n pyre-redistest -g rg-pyre-redistest -l westus2 --sku Balanced_B0
```

- **Succeeds** → that region works. Clean up and use it as your `location`:
  ```powershell
  az group delete -n rg-pyre-redistest --yes
  ```
- **Fails** → try another (`centralus`, `eastus`, `westus3`) until one places it,
  and set `location` in `dev.tfvars` to the winner.

### 5c. Bootstrap remote Terraform state (once)

Production keeps state in Azure, one blob per instance. Terraform can't create the
account it stores its own state in, so you make it by hand, once:

```powershell
az group create -n rg-pyre-tfstate -l westus2
$STATE = "pyretfstate$(Get-Random -Maximum 100000)"    # globally unique
az storage account create -n $STATE -g rg-pyre-tfstate -l westus2 `
  --sku Standard_LRS --min-tls-version TLS1_2 --allow-blob-public-access false
az storage container create -n tfstate --account-name $STATE --auth-mode login
$STATE      # <- put THIS into infra/backend.tf (storage_account_name)
```

Open `infra/backend.tf`, set `storage_account_name` to that name and
`resource_group_name = "rg-pyre-tfstate"`.

### 5d. Create the instance RG and grant yourself the deploy roles

Shared keys are off, so the deployer reaches storage's blob **data** plane by
identity — and subscription **Owner** is control-plane only, so that data role is
separate. Grant it to your user (and later to the deployer VM's identity, 5.0).
You also stand in as the log sender + publisher:

```powershell
az group create -n rg-pyre-dev -l westus2
az provider register -n Microsoft.Storage --wait
$SUB = az account show --query id -o tsv
$OID = az ad signed-in-user show --query id -o tsv     # also goes in dev.tfvars
az role assignment create --assignee $OID --role "Storage Blob Data Contributor" `
  --scope "/subscriptions/$SUB/resourceGroups/rg-pyre-dev"
# wait ~1–2 min for it to propagate  (without it: 403 KeyBasedAuthenticationNotPermitted)
```

### 5e. Fill in `infra/envs/dev.tfvars`

The file is already written for you — replace the placeholders and confirm
`location` matches your verified region. There is **no** IP allowlist: everything
is private, so you run the cloud steps from the deployer VM (5.0).

```hcl
location            = "westus2"                # the region you verified in 5b
owner               = "<your object id>"       # $OID from 5d
admin_principal_ids = ["<your object id>"]      # who may change this instance; $OID from 5d
log_sender = { mode = "managed_identity", principal_id = "<your object id>" }
publisher  = { mode = "managed_identity", principal_id = "<your object id>" }
```

> **Who can make changes.** `admin_principal_ids` is how you add trusted operators
> using Azure's own RBAC: each object id listed gets Contributor + User Access
> Administrator on this instance's resource group. In the company, list an **Entra
> group** id instead and add/remove people by group membership — no terraform run.
> Adding *yourself* here (your `$OID`) is what lets you re-apply. The first apply is
> bootstrapped by your subscription Owner rights (5d).

### 5f. Init, plan, apply

```powershell
cd infra
terraform init -backend-config="key=dev.tfstate"
terraform plan  -var-file="envs/dev.tfvars"       # READ IT — every service should show public_network_access_enabled = false or Deny-by-default
terraform apply -var-file="envs/dev.tfvars"
```

**10–20 minutes** — private endpoints and DNS propagation are the slow part.

```powershell
terraform -chdir=infra output
```

Note `eventhub_hub_names` and `default_eventhub_name` (`default-logs-in`). That
is the hub your `ICO.Network` logs belong on — they have no dedicated hub, so
they ride the catch-all, and are evaluated there exactly the same.

## 6. Deploy the engine and the mock sink

```powershell
cd engine
func azure functionapp publish pyredev1-proc --python
cd ..
cd tools/mocks/mock_destination
func azure functionapp publish pyredev1-mockdest --python
cd ../../..
```

> Run this from the **deployer VM** (5.0): the Function App and storage are
> private, so `func publish` only reaches them from inside the VNet. Same for
> `pyre publish` and the log shipper below. This is exactly the company posture.

## 7. Point the engine at the mock

```hcl
# add to infra/envs/dev.tfvars
destinations   = { mock = { url = "https://pyredev1-mockdest.azurewebsites.net/api/alert" } }
default_routes = ["mock"]
```

```powershell
terraform -chdir=infra apply -var-file=envs/dev.tfvars
az functionapp restart -n pyredev1-proc -g rg-pyre-dev
```

**The restart matters.** Destinations are read at **startup**; only detections
hot-reload.

## 8. Publish your detection

```powershell
$env:BUNDLE_BLOB_ACCOUNT_URL = "https://pyredev1stor.blob.core.windows.net"
python cli/pyre pull
python cli/pyre build
python cli/pyre publish
```

`publish` uploads a versioned zip **then** flips a pointer last, so a worker can
never see a pointer to a bundle that isn't there. Warm workers reload within 45s.

## 9. Feed it and watch it work

```powershell
python tools/testlab/python_shipper.py `
  --namespace pyredev1-ehns.servicebus.windows.net `
  --hub default-logs-in `
  --file network_logs.jsonl --rate 10
```

It authenticates with your `az login` (Entra) — which works because you granted
yourself Data Sender in Part 5.

**Fire it on purpose.** Simplest on Windows: append the synthetic port-4444 line
and re-ship (there is no `nc` on Windows; if you want a *real* outbound 4444
connection to capture, `Test-NetConnection <ip> -Port 4444` attempts one).

```powershell
'{"dataset":"ICO.Network","_time":"2026-07-15T00:00:00Z","remote_ip":"203.0.113.9","remote_port":4444,"process_name":"nc","hostname":"test-host"}' | Add-Content network_logs.jsonl
python tools/testlab/python_shipper.py --namespace pyredev1-ehns.servicebus.windows.net --hub default-logs-in --file network_logs.jsonl --rate 10
```

**Where to look:**

- **`pyredev1-ehns` → Overview** — incoming messages. Flat = nothing arrived, and
  the problem is upstream of pyre entirely.
- **`pyredev1-mockdest` → Monitor / Logs** — **your alert's payload.** This is the
  proof: network log → Event Hubs → engine → match → dedup → dispatch → sink.
- **`pyredev1-law` (Log Analytics) → Logs**:

```kql
traces | where timestamp > ago(15m) | order by timestamp desc
```

**Run this too — it is the "why isn't it working" query:**

```kql
traces
| where timestamp > ago(1h)
| where message has_any (
    "batch dropped",                 // your events had no `dataset`
    "matches no enabled detection",  // `dataset` matches no detection's LogTypes
    "skipping detection",            // your rule failed to import
    "failed to deliver",             // the mock URL is wrong / app not restarted
    "SIGNALS_SINK_URL is not set")   // EXPECTED in this lab — you have no lake
| order by timestamp desc
```

`SIGNALS_SINK_URL is not set` is **expected here**: you have no Cribl lake, so
signals are discarded and only alerts reach the mock. Fine for a lab; in
production that line means your entire audit trail is going nowhere.

## 10. Change the detection and watch it hot-reload

**This is the payoff — the whole reason detections live in a separate repo.**

In `pyre-dac`, widen the rule:

```python
def rule(event):
    return event.get("remote_port") in (4444, 8080)
```

```powershell
git commit -am "Also alert on port 8080"; git push
cd ../pyre
python cli/pyre pull; python cli/pyre build; python cli/pyre publish
```

Wait ~45s. **Do not redeploy or restart anything.** Ship a log with
`remote_port: 8080` and watch it fire. Push to live in under a minute, no deploy
— across every warm worker, at any scale.

## 11. Tear it down

**Do this. Today.**

```powershell
terraform -chdir=infra destroy -var-file=envs/dev.tfvars
```

This removes every billed resource — Redis and Event Hubs, the expensive ones.
Your detections live in Git, so nothing of value is lost — Parts 5–8 rebuild the
identical thing. Two things `destroy` does NOT touch, because they live in
separate resource groups it doesn't manage:

```powershell
az group delete -n rg-pyre-dev      --yes   # the instance RG (should be empty after destroy)
az group delete -n rg-pyre-tfstate  --yes   # the remote-state account — ONLY if you're fully done
```

---

## When it doesn't work

| Symptom | Cause |
|---|---|
| `batch dropped N/N ... missing the 'log type' field` | Your events have no `dataset`. Re-check Part 3. |
| `matches no enabled detection` | `dataset` on your logs ≠ `LogTypes:` in your `.yml`. Must match **exactly**. |
| Signals but no alert | Below `Threshold`, or grouped into an existing alert by `DedupPeriodMinutes`. Working as designed. |
| Alert fires but nothing in the mock | `destinations.mock.url` wrong, or you didn't **restart** the app after setting it (Part 7). |
| `skipping detection <id>` | Your rule won't import — missing helper, or a package the engine lacks. Run `python cli/pyre deps`. |
| `func publish` hangs or times out | You're running it from your laptop, not the VNet deployer VM (5.0). The Function App is private — `func publish` only reaches it from inside the VNet. |
| Detection change never went live | You forgot `pyre publish`, or it's been <45s. `pull`+`build` alone change nothing in Azure. |
| Everything green, nothing happens | Check the hub. `--hub` must be one of `terraform output eventhub_hub_names`. A hub nothing consumes accepts every event and evaluates none, silently. |
| `pyre publish` → 403 | `publisher.principal_id` isn't your object ID, or that apply hasn't run. |
| `terraform init` errors on the backend | `storage_account_name` in `infra/backend.tf` doesn't match the state account you created in 5c (or the container/RG is wrong). |
| Managed Redis create fails with a generic `OperationFailed` | Regional capacity for `Balanced_B0`. That's what 5b catches — pick a region that places it. |

## Going further (what changes for the company)

- **Give a source its own hub, or its own namespace.** In `config/sources.yaml`,
  add a hub under a namespace for isolation + more partitions, or add a whole
  `namespace:` block. Set `create: false` + `existing_name`/`existing_resource_group`
  to **bind** a namespace that already exists (e.g. Azure diagnostics already in an
  Event Hub) instead of creating one. Each namespace has its own `shape`
  (log-type/time/envelope), so **one** Function App consumes Cribl and Azure-native
  feeds at once. `terraform apply` and the processor picks up the new hub — no
  engine change.
- **Disable a detection.** Set `Enabled: false` in its `.yml` in `pyre-dac` and
  `pyre publish`. The engine drops it at load, so it is never evaluated; it
  reloads within `refresh_interval_seconds`. (There is no separate on/off command.)
- **Route to Torq/Cribl.** Add the destination to `config/destinations.yaml`
  (`kind: torq`), put its token in the engine Key Vault, and name it in
  `default_routes`. Every destination receives the full alert payload — the raw
  `event`, its `p_fields`, and `rule_id`/`severity`/`alert_context`.

## What this lab does not cover

Cribl (you stamped `dataset` yourself), a real destination (mock only), the
signals lake (`signals_sink_url` unset), and anything about scale. This deploys
the real secure architecture — fully private, nothing exposed — but it's still a
personal instance: your own subscription, a mock destination, no lake, and a
throwaway deployer VM standing in for a company CI agent. Before a company
deployment, work [PRODUCTION_CHECKLIST.md](PRODUCTION_CHECKLIST.md).
