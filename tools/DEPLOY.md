# Deploy the pyre infrastructure

Stand up the whole private Azure stack — Event Hubs, Redis, the Function App, two
Key Vaults, storage, VNet + private endpoints — in your subscription.

**`terraform apply` runs from your laptop with only your `az login`.** Everything
is private (no public endpoints, no IP allowlist); Terraform reaches Azure through
the management plane (ARM), which is auth-gated, so who can deploy = who can
authenticate with the right RBAC. Nothing about IPs.

> **What this guide does NOT do:** push the engine *code* (`func publish`) or
> *logs* into the private resources. Those hit each resource's own private
> endpoint, so they run from inside the VNet (a CI agent / jumpbox) — separate
> from standing up the infrastructure. This guide is the infrastructure.

---

## 0. Prerequisites

- **`az login`** as a subscription **Owner** — Terraform creates role assignments,
  which needs Owner (or User Access Administrator).
- **Terraform ≥ 1.7**.
- One-time provider registration (silent if already done):
  ```powershell
  az provider register -n Microsoft.Cache --wait      # Managed Redis
  az provider register -n Microsoft.EventHub --wait
  ```

## 1. Pick a region that has Managed Redis capacity — DO NOT SKIP

`Balanced_B0` (the cheapest Redis SKU) is capacity-constrained and **fails to
allocate in some regions on some days** — the error is `AllocationFailed:
insufficient capacity`, and a bigger size in the same region usually fails too.
On 2026-07-16, eastus2 and westus3 both failed while **eastus** worked. This is
the single most likely thing to blow up a 20-minute apply, so prove your region
in ~2 min and pennies first. `--public-network-access` is now required by the CLI:

```powershell
az group create -n rg-pyre-redistest -l eastus
az redisenterprise create -n pyre-redistest-$(Get-Random) -g rg-pyre-redistest -l eastus --sku Balanced_B0 --public-network-access Disabled
# state "Succeeded" -> use this region. If "Failed" with AllocationFailed, try another region.
# clean up either way: az group delete -n rg-pyre-redistest --yes
```
```bash
az group create -n rg-pyre-redistest -l eastus
az redisenterprise create -n pyre-redistest-$RANDOM -g rg-pyre-redistest -l eastus --sku Balanced_B0 --public-network-access Disabled
# state "Succeeded" -> use this region. If "Failed" with AllocationFailed, try another region.
# clean up either way: az group delete -n rg-pyre-redistest --yes
```

## 2. Set your variables — `infra/envs/dev.tfvars`

Copy the example and fill it in. Every value and why you set it:

```hcl
location            = "eastus2"        # the region you verified in step 1
resource_group_name = "rg-pyre-dev"    # the RG this instance deploys into (you create it in step 3)
name_prefix         = "pyredev"        # prefix on EVERY resource name; must be globally unique
                                       # (storage/KV/Event Hubs names are global). Lowercase, short.
env                 = "dev"            # tag + PYRE_ENV; does NOT drive routing
cost_profile        = "test"           # cheapest SKUs (Redis Balanced_B0, 1 TU Event Hubs, Flex to 40)
                                       #   "scale" = production SKUs (Balanced_B5, auto-inflate, Flex to 1000)
key_vault_purge_protection = false     # false for a rebuildable dev instance; true (default) in prod

# Who may change this instance (RBAC). Your own object id here; an Entra GROUP id
# at the company (then add people by group membership, no terraform run).
#   az ad signed-in-user show --query id -o tsv
admin_principal_ids = ["<your object id>"]

# The identities pyre trusts, for a laptop deploy both are YOU (your az login
# stands in as the log sender and the bundle publisher). "managed_identity" mode
# just means "an object id that already exists".
log_sender = { mode = "managed_identity", principal_id = "<your object id>" }
publisher  = { mode = "managed_identity", principal_id = "<your object id>" }
```

What the log sources look like is `config/sources.yaml` — the default is one
`cribl` namespace with three hubs (palo / cloudflare / a catch-all). You don't
touch it to deploy; edit it to add a hub or a whole namespace.

## 3. Create the RG and apply

One resource group, and nothing to bootstrap — state is kept locally.

```powershell
az group create -n rg-pyre-dev -l eastus2
terraform -chdir=infra init
terraform -chdir=infra plan  -var-file=envs/dev.tfvars   # READ IT: every service shows public_network_access_enabled = false
terraform -chdir=infra apply -var-file=envs/dev.tfvars
```
```bash
az group create -n rg-pyre-dev -l eastus2
terraform -chdir=infra init
terraform -chdir=infra plan  -var-file=envs/dev.tfvars
terraform -chdir=infra apply -var-file=envs/dev.tfvars
```

**~15–25 minutes** — Managed Redis provisioning is the long pole; private
endpoints + DNS are the rest.

## 4. See what you deployed

```powershell
terraform -chdir=infra output
az resource list -g rg-pyre-dev -o table
```

You should see (all private):

| Resource | Name |
|---|---|
| Virtual network + 2 subnets | `<prefix>-vnet` (snet-pe, snet-functions) |
| 4 private DNS zones | eventhub / redis / keyvault / blob |
| User-assigned Managed Identity | `<prefix>-mi` (the processor) |
| Event Hubs namespace + 3 hubs + PE | `<prefix>-cribl-ehns` |
| Azure Managed Redis + PE | `<prefix>-redis` |
| 2 Key Vaults + PEs | `<prefix>-kv` (engine), `<prefix>-ci-kv` (CI) |
| Storage account + 3 containers + PE | `<prefix>stor` (checkpoints/bundle/detections) |
| Log Analytics + App Insights | `<prefix>-law` |
| Flex Consumption plan + Function App | `<prefix>-flex-plan`, `<prefix>-proc` |
| Role assignments | processor + admin + sender/publisher grants |

Every one shows `Public network access: Disabled` in the portal — reached only
via its private endpoint, from inside the VNet.

## 5. Tear it down

One RG, so one delete. Managed Redis bills hourly, so destroy when you're done:

```powershell
terraform -chdir=infra destroy -var-file=envs/dev.tfvars
az group delete -n rg-pyre-dev --yes
```
```bash
terraform -chdir=infra destroy -var-file=envs/dev.tfvars
az group delete -n rg-pyre-dev --yes
```

---

## Notes

- **No IP allowlist anywhere.** Access is Azure identity (RBAC) + private VNet.
  Add a deployer = add their object id to `admin_principal_ids` (or the Entra
  group it points at).
- **A second instance** (dev + prod, or per-team) is just a different `.tfvars`
  with a different `name_prefix` — same composition, names stay distinct.
- **Running the engine** (deploy code, feed logs, watch a detection fire) is the
  next step and needs in-VNet access; see `tools/LAB.md`.
