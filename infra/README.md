# infra/ — the Azure environment, as code (Terraform)

This directory is the **recipe for the cloud**. Instead of clicking around the Azure portal, you describe the resources you want in `.tf` files and **Terraform** makes Azure match. Change a file, re-run, and it updates only what changed. Delete everything with one command when you're done.

Never used Terraform? Read the [glossary](../docs/GLOSSARY.md) entries for *Terraform, module, state, plan/apply/destroy, tfvars* first. Then this page. Then, to actually deploy, follow [docs/PRODUCTION.md § 9](../docs/PRODUCTION.md#9-spin-up-cloud) — that's the step-by-step; this page explains *what you're looking at*.

## One composition, many instances

There is **one** definition of the whole system (the `.tf` files in this folder). You deploy it as many times as you want — a dev instance, a prod instance, a per-team instance — where **an "instance" is just a different `.tfvars` file plus its own state key**. dev is simply prod with cheaper values.

```
infra/
  main.tf         the composition — wires the modules together (one of everything)
  variables.tf    every input, with sensible defaults; only location + rg are required
  outputs.tf      handy values after apply (function app name, event hub fqdn, …)
  providers.tf    Terraform + azurerm provider versions
  backend.tf      remote state config (one state blob per instance, keyed at init)
  envs/           per-instance values — NOT code, just settings:
    dev.tfvars.example    the dev instance (cheapest SKUs, 10.0.0.0/16)
    prod.tfvars.example   the prod instance (scaled SKUs, 10.1.0.0/16)
  modules/        reusable building blocks — one concern each (see table below)
  global/         one-time remote-state bootstrap
```

**Module vs. composition vs. instance:** a *module* is a reusable block ("how to make a Redis, done right"). The *composition* (`main.tf`) says "make one of each and connect them." An *instance* is one deployment of that composition with a specific `.tfvars`. Modules couple only through outputs, so any one can be replaced without touching the others.

## Instances (dev and prod are just two `.tfvars`)

Same composition, same private posture — only the values differ:

| Value | dev (`envs/dev.tfvars`) | prod (`envs/prod.tfvars`) |
|---|---|---|
| `name_prefix` | `pyredev` | `pyreprod` (must be globally unique per instance) |
| `cost_profile` | `test` (cheapest SKUs) | `scale` (Premium Redis, auto-inflating Event Hubs, Flex to 1000) |
| `address_space` | `10.0.0.0/16` | `10.1.0.0/16` (distinct so they can peer) |
| state key | `dev.tfstate` | `prod.tfstate` (same store, separate blobs) |
| Network posture | fully private | fully private (identical) |

Both use **Managed Identity + Key Vault** (never passwords in files) and are fully private (no public access; logs arrive via Cribl). To make a third instance, copy a `.tfvars`, change `name_prefix`/`env`/CIDR, and deploy with a new state key. See [docs/PRODUCTION.md § 5](../docs/PRODUCTION.md#5-environments) for the full comparison (including the `local` laptop environment) and cost.

## What each module creates (and the one thing to know)

| Module | Creates | Key point |
|---|---|---|
| `network` | VNet, two subnets (Functions-delegated + private-endpoints), private DNS zones | The private network everything else attaches to. |
| `identity` | One user-assigned Managed Identity | The single identity the engine uses to reach Event Hub, Redis, Blob, and Key Vault. |
| `storage` | Storage account with `checkpoints`, `bundle`, `detections` containers | `detections` holds published detection bundles; `bundle` is the Function App's own deploy package. A CI identity may write `detections` (`publisher_principal_id`). |
| `eventhub` | Event Hubs namespace + the `logs-in` hub, sized from `config/sources.yaml` | Grants the engine's MI *receive* and Cribl's identity *send* — no access keys. |
| `redis` | Azure Cache for Redis + a **data-plane access policy** for the engine's MI | That access policy is what lets the engine actually use Redis (Entra auth, no keys). Biggest single cost. |
| `keyvault` | **Two** Key Vaults (the module is instantiated twice) | `module.keyvault` holds engine runtime secrets (Torq tokens, Cribl creds) - readable only by the processor MI. `module.ci_keyvault` holds CI-only secrets - readable only by the publisher service connection. Two vaults, not one with two roles, so a compromised identity on one side can't read the other's secrets. |
| `function_app` | The Flex Consumption Function App (the processor) + all its settings | Reads Event Hub/Redis/Key Vault/Blob **by identity**. |
| `monitoring` | Log Analytics workspace + Application Insights | Where the engine's logs and metrics land. |

## How to read it

Start at `main.tf` to see the whole system assembled from modules, then open any module's `main.tf` for its details. Each module has `variables.tf` (inputs), `main.tf` (resources), `outputs.tf` (values it returns).

## Deploy / change / destroy (quick reference — full steps in [PRODUCTION.md](../docs/PRODUCTION.md))

One-time: bootstrap the remote state store (`infra/global/backend.tf.example`), and `cp envs/dev.tfvars.example envs/dev.tfvars` and fill it in. Then, from the repo root:

```bash
terraform -chdir=infra init  -backend-config="key=dev.tfstate"   # once per instance
terraform -chdir=infra plan    -var-file=envs/dev.tfvars         # preview (read this!)
terraform -chdir=infra apply   -var-file=envs/dev.tfvars         # make it
terraform -chdir=infra destroy -var-file=envs/dev.tfvars         # delete it (your off switch)
```

For prod, swap `dev` → `prod` everywhere. Or use the Makefile: `make apply ENV=dev` / `make apply ENV=prod` — same targets, `ENV` picks the instance.

- **State:** remote, one blob per instance (the `key`). Bootstrap once with `global/`.
- **`.tfvars` are gitignored** (they hold instance specifics); only the `.example` files are committed.
- **Formatting/validation:** `terraform -chdir=infra fmt -recursive` and `terraform -chdir=infra validate` — both run clean; CI runs them too.

For the full guided deploy, updating, monitoring, and teardown, see **[docs/PRODUCTION.md](../docs/PRODUCTION.md)**.
