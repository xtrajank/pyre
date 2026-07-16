# All inputs to the pyre composition. An "instance" (dev, prod, team-x, …) is just
# a different set of these values in a .tfvars file. There is ONE composition; you
# stamp out as many instances as you want. Only `location` and `resource_group_name`
# are required — everything else has a sensible default you override per instance.

variable "name_prefix" {
  type        = string
  default     = "pyre"
  description = <<-EOT
    Prefix for EVERY resource name — this is what makes each instance distinct.
    Some services (Storage, Key Vault, Event Hubs) require globally-unique names,
    so each instance MUST use a different prefix (e.g. "pyredev", "pyreprod").
    Lowercase letters/digits; keep it short (storage account name = "<prefix>stor").
  EOT
}

variable "env" {
  type        = string
  default     = "dev"
  description = "Environment label. Tags resources and sets PYRE_ENV. It does NOT drive alert routing - that is `default_routes`, explicitly, per instance."
}

variable "location" {
  type        = string
  description = "Azure region, e.g. eastus2."
}

variable "resource_group_name" {
  type        = string
  description = "Resource group to deploy this instance into (e.g. rg-pyre-dev)."
}

variable "cost_profile" {
  type        = string
  default     = "test"
  description = "test = cheapest SKUs (Azure Managed Redis Balanced_B0, Event Hubs 1 TU, Flex 0 always-ready). scale = larger Managed Redis (Balanced_B5 floor), auto-inflating Event Hubs, Flex up to 1000 instances."
}

variable "owner" {
  type    = string
  default = "secops"
}

variable "key_vault_purge_protection" {
  type        = bool
  default     = true
  description = <<-EOT
    Purge protection on both vaults. true (default, prod): a soft-deleted vault
    can't be purged for 90 days, so secrets survive an accidental delete - but the
    name is reserved for 90 days too, blocking a same-name rebuild. false (dev):
    purge immediately and reuse the name. Never false in production.
  EOT
}

# NOTE: there is no deployer IP allowlist. Every resource is always private,
# reached only from inside the VNet. Deploy from a VNet-resident agent/VM; RBAC
# governs the deployer's identity, VNet membership governs reachability.

# --- Network (give each instance its own CIDR if they will peer) --------------
variable "address_space" {
  type    = string
  default = "10.0.0.0/16"
}
variable "pe_subnet_prefix" {
  type    = string
  default = "10.0.1.0/24"
}
variable "functions_subnet_prefix" {
  type    = string
  default = "10.0.2.0/24"
}

# --- External identities to trust ---------------------------------------------
# pyre trusts exactly two actors outside its own processor Managed Identity:
# whatever sends logs into Event Hubs (your log shipper/router - Cribl, Fluent
# Bit, a custom forwarder, ...) and whatever CI identity runs `pyre publish`.
# Neither is assumed to be any specific product or platform. Each is described
# by a `mode`, not a fixed mechanism, because "how does this actor prove who it
# is to Azure" only ever has two shapes:
#   mode = "managed_identity" - the actor IS an Azure resource (a VM, VMSS,
#     Container App, AKS pod, a self-hosted CI agent on Azure compute, ...)
#     and already has an identity attached. Set principal_id to that
#     identity's object ID; nothing else is created.
#   mode = "federated"         - the actor is NOT an Azure resource (a
#     laptop, GitHub Actions, ADO Microsoft-hosted agents, any OIDC-capable
#     CI). Set federated_credentials to the issuer/subject its OIDC token
#     presents; Terraform provisions a user-assigned identity plus the trust
#     (module.external_identity) so it authenticates with no stored secret.
# See infra/modules/external_identity for the mechanics, and
# infra/README.md / docs/PRODUCTION.md § 9.5 for worked examples of both.
variable "log_sender" {
  type = object({
    mode         = optional(string, "managed_identity")
    principal_id = optional(string, "")
    federated_credentials = optional(list(object({
      name     = string
      issuer   = string
      subject  = string
      audience = optional(list(string), ["api://AzureADTokenExchange"])
    })), [])
  })
  default     = {}
  description = "The identity of whatever sources/sends logs into Event Hubs. Leave principal_id empty (the default) to skip granting Send access until it's known."
}
variable "publisher" {
  type = object({
    mode         = optional(string, "managed_identity")
    principal_id = optional(string, "")
    federated_credentials = optional(list(object({
      name     = string
      issuer   = string
      subject  = string
      audience = optional(list(string), ["api://AzureADTokenExchange"])
    })), [])
  })
  default     = {}
  description = "The CI identity that runs `pyre publish` (writes detection bundles to the `detections` Blob container and reads the CI-only Key Vault). Leave principal_id empty (the default) to skip granting write access until it's known."
}

# NOTE: feed shape (log-type field, event-time field, envelope) is now declared
# PER NAMESPACE in config/sources.yaml, so one instance can consume feeds of
# different shapes. It is no longer a root variable.

# --- Governance: who may change this instance ---------------------------------
variable "admin_principal_ids" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Entra OBJECT IDs allowed to change this instance (run terraform apply). A
    user, group, or service-principal object id - company users already exist, so
    map them by id (prefer a group: then adding a person is group membership, no
    terraform run). Each is granted Contributor + User Access Administrator on
    the resource group. Empty (default) grants nobody via Terraform.
    Your own id: az ad signed-in-user show --query id -o tsv
  EOT
}

# --- Engine tuning ------------------------------------------------------------
variable "refresh_interval_seconds" {
  type    = number
  default = 45 # how fast a detection push goes live on a warm worker
}
variable "storm_limit" {
  type    = number
  default = 1000 # alerts/detection/hour before the storm limiter suppresses dispatch
}
variable "idempotency_ttl_seconds" {
  type        = number
  default     = 900
  description = <<-EOT
    How long a processed event id is remembered so an at-least-once redelivery
    isn't counted twice. THE MAIN REDIS SIZING KNOB: the only key written per
    EVENT, so resident keys ~= events/sec x this (~50k/s x 900s ~= 45M keys,
    ~4-5GB). It only has to outlive a checkpoint retry (seconds-minutes), so 15
    min is generous. Too low -> a late redelivery duplicates signals (alerts still
    collapse on dedup).
  EOT
}
variable "worker_process_count" {
  type        = number
  default     = 4
  description = <<-EOT
    Worker PROCESSES per instance (FUNCTIONS_WORKER_PROCESS_COUNT). rule() is
    CPU-bound, so processes - not threads - buy real parallelism (threads share
    one GIL). Each process holds its own Processor (bundle + Redis pool), trading
    instance_memory_in_mb for CPU.
  EOT
}
variable "threads_per_worker" {
  type        = number
  default     = 4
  description = "Threads per worker process (PYTHON_THREADPOOL_THREAD_COUNT): concurrent batches sharing one Processor. Buys overlap across Redis/Cribl waits, not CPU. Raise only if workers are I/O-parked, not pegged."
}
variable "max_event_batch_size" {
  type        = number
  default     = 256
  description = <<-EOT
    Max events per invocation. 256 = batched (scalable default); 1 = single-event
    (low-volume, latency-critical). A CEILING, not a wait - small backlogs deliver
    immediately, so a high value never delays alerts at low volume.
  EOT
}
variable "signals_sink_url" {
  type    = string
  default = "" # Cribl HTTP source for the signals/alerts write-back
}

# --- Alert destinations -------------------------------------------------------
variable "destinations" {
  type = map(object({
    url          = optional(string, "")
    token_secret = optional(string, "")
  }))
  default     = {}
  description = <<-EOT
    Per-instance destination values, keyed by the `name` in
    config/destinations.yaml (the `kind` and adapter live there / in dispatch.py,
    so nothing here is vendor-named). Each entry becomes:
        DESTINATION_<NAME>_URL     = url                        (not a secret)
        DESTINATION_<NAME>_TOKEN   = KV reference to token_secret
    token_secret is the SECRET NAME in the engine's Key Vault, not the token; omit
    it for an auth-less destination. A dev instance declares only its mock sink
    and NO production destination - that is what keeps it unable to page prod.

    Example:
      destinations = {
        mock      = { url = "https://<mock-func>/api/alert" }
        torq_prod = { url = "https://<torq-webhook>", token_secret = "torq-prod-token" }
      }
  EOT
}

variable "default_routes" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Destinations an alert goes to when its detection names none (a rule's
    destinations(event) overrides). Values are `name`s from
    config/destinations.yaml. THE CONTROL THAT KEEPS A LAB OFF PRODUCTION: routing
    is explicit per instance, not inferred from `env`. Empty = such alerts are not
    delivered (loudly, at cold start), almost always a misconfiguration.
  EOT
}

# --- Event Hubs scaling (only used when cost_profile = "scale") ---------------
variable "throughput_units_floor" {
  type    = number
  default = 1 # guaranteed throughput units (each ~= 1 MB/s in, 2 MB/s out)
}
variable "max_throughput_units" {
  type    = number
  default = 20 # auto-inflate ceiling under sustained load
}
