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
  description = "Environment label. Tags resources and sets PYRE_ENV, which drives default alert routing (dev -> mock, otherwise -> Torq)."
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
  description = "test = cheapest SKUs (Redis Basic C0, Event Hubs 1 TU, Flex 0 always-ready). scale = Premium clustered Redis, auto-inflating Event Hubs, Flex up to 1000 instances."
}

variable "owner" {
  type    = string
  default = "secops"
}

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

# --- Feed field mapping ---------------------------------------------------
# Which field on each incoming event the engine reads to (a) pick which
# detections apply and (b) stamp the time on signals/alerts. This repo's
# reference normalizer is Cribl, whose own field names are `dataset` (not
# Panther's `p_log_type` convention) and `_time` - those are the defaults
# below. Any feed can override both: point log_type_field at whatever field
# your normalizer uses to mark log type (Cribl: dataset; a Panther-style
# pipeline: p_log_type; your own: whatever you call it), and event_time_field
# at whatever carries the event's original timestamp (Cribl: _time; Panther-
# style: p_event_time). This is safe to change freely: both are read with a
# safe default/skip (see engine/pyre_engine/processor.py) - a misconfigured
# name degrades to "no detections match" or "blank timestamp," never a
# security-relevant failure, since alert dedup/threshold windowing uses Redis
# TTLs, not these fields.
variable "log_type_field" {
  type        = string
  default     = "dataset"
  description = "Event field the engine reads to select which detections run (Cribl's own field is `dataset`; a Panther-style feed would use `p_log_type`)."
}
variable "event_time_field" {
  type        = string
  default     = "_time"
  description = "Event field the engine reads for the signal/alert timestamp (Cribl's own field is `_time`; a Panther-style feed would use `p_event_time`)."
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
variable "max_event_batch_size" {
  type        = number
  default     = 256
  description = <<-EOT
    Max events the engine processes per invocation — the batch<->single-event knob.
    256 = batched (scalable default). 1 = single-event (one invocation per event;
    for a low-volume, latency-critical instance where cost doesn't matter).
    This is a CEILING, not a wait: small backlogs are delivered immediately, so a
    high value does not delay alerts at low volume. Latency is dominated by cold
    starts and Cribl's flush interval, not this — see docs/PRODUCTION.md § 15.
  EOT
}
variable "signals_sink_url" {
  type    = string
  default = "" # Cribl HTTP source for the signals/alerts write-back
}
variable "mock_dest_url" {
  type    = string
  default = "" # optional non-Torq alert sink (webhook/test mock)
}
variable "torq_dev_url" {
  type    = string
  default = "" # Torq webhook URL for torq_dev (config/destinations.yaml). Not secret; the token below is.
}
variable "torq_prod_url" {
  type    = string
  default = "" # Torq webhook URL for torq_prod
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
