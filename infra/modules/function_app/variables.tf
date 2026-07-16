variable "name_prefix" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "cost_profile" { type = string }
variable "env" { type = string }
variable "identity_id" { type = string }
variable "identity_client_id" { type = string }
variable "functions_subnet_id" { type = string }
variable "deploy_container_endpoint" { type = string }
# name -> { fqdn } for every namespace the processor consumes (created + bound).
# Each becomes an EVENTHUB_<NAME> Managed-Identity trigger connection.
variable "namespaces" {
  type = map(object({ fqdn = string }))
}
# Every hub the processor subscribes to, with its namespace and shape. The engine
# registers one Event Hubs trigger per entry (HUBS_CONFIG). Derived from
# config/sources.yaml, never hand-written: a hub that isn't real binds to
# nothing, a real hub missing here ingests at full rate and is evaluated by
# nobody - both silent.
variable "hubs" {
  type = list(object({
    hub              = string
    namespace        = string
    log_type_field   = string
    event_time_field = string
    envelope         = string
  }))
  validation {
    condition     = length(var.hubs) > 0
    error_message = "the processor must consume at least one hub, or nothing is ever evaluated."
  }
}
variable "redis_host" { type = string }
variable "redis_ssl_port" { type = number }
variable "kv_uri" { type = string }
variable "bundle_blob_account_url" { type = string }
variable "refresh_interval_seconds" {
  type    = number
  default = 45
}
variable "signals_sink_url" {
  type    = string
  default = ""
}
variable "storm_limit" {
  type    = number
  default = 1000
}
variable "idempotency_ttl_seconds" {
  type    = number
  default = 900 # per-EVENT Redis key TTL: the main Redis memory driver (see root variables.tf)
}
variable "worker_process_count" {
  type    = number
  default = 4 # OS processes per instance — the real parallelism (each has its own GIL)
}
variable "threads_per_worker" {
  type    = number
  default = 4 # threads per process — concurrent batches sharing one Processor
}
variable "app_insights_conn" {
  type    = string
  default = ""
}
variable "destinations" {
  type = map(object({
    url          = optional(string, "")
    token_secret = optional(string, "")
  }))
  default = {}
  # Per-instance alert destination values, keyed by the `name` in
  # config/destinations.yaml. Deliberately NOT named after any tool: each entry
  # becomes DESTINATION_<NAME>_URL and (if token_secret is set) a Key Vault
  # reference at DESTINATION_<NAME>_TOKEN. The kind (torq/webhook/mock) is
  # declared in destinations.yaml; the adapter lives in dispatch.py. See the
  # root variables.tf for the full contract.
}
variable "default_routes" {
  type    = list(string)
  default = []
  # Destinations an alert goes to when its detection names none. Replaces a
  # hardcoded env=="dev" check in the engine - see root variables.tf.
}
variable "max_event_batch_size" {
  type    = number
  default = 256 # ceiling of events per invocation. 1 = single-event; 256 = batched (default)
}
variable "tags" {
  type    = map(string)
  default = {}
}
