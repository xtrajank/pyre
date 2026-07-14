variable "name_prefix" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "cost_profile" { type = string }
variable "env" { type = string }
variable "identity_id" { type = string }
variable "identity_client_id" { type = string }
variable "functions_subnet_id" { type = string }
variable "deploy_container_endpoint" { type = string }
variable "eventhub_name" { type = string }
variable "eventhub_fqdn" { type = string }
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
variable "app_insights_conn" {
  type    = string
  default = ""
}
variable "mock_dest_url" {
  type    = string
  default = "" # optional non-Torq alert sink (e.g. a webhook or the test mock); "" in prod
}
variable "max_event_batch_size" {
  type    = number
  default = 256 # ceiling of events per invocation. 1 = single-event; 256 = batched (default)
}
variable "tags" {
  type    = map(string)
  default = {}
}
