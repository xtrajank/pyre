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

# --- Identities to grant (object IDs from Entra) ------------------------------
variable "cribl_sender_principal_id" {
  type    = string
  default = "" # Cribl's identity, so it may SEND logs to Event Hubs
}
variable "publisher_principal_id" {
  type    = string
  default = "" # Azure Pipelines Workload Identity Federation service connection that runs `pyre publish`
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
