variable "name_prefix" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "cost_profile" { type = string }

# The namespaces to CREATE, each with its hubs. Existing namespaces are bound in
# infra/main.tf (data source + role grant), not here. Keyed by namespace name.
variable "namespaces" {
  type = map(object({
    hubs = map(object({ partitions = number, retention_hours = number }))
  }))
}

variable "pe_subnet_id" { type = string }
variable "dns_zone_id" { type = string }
variable "processor_principal_id" { type = string }
variable "sender_principal_id" {
  type    = string
  default = ""
}
# Whether sender_principal_id names a real identity. Separate from the id because
# a federated sender's id is unknown at plan time and a count/for_each key must
# be known. See local.log_sender_enabled in infra/main.tf.
variable "sender_enabled" {
  type    = bool
  default = false
}
variable "throughput_units_scale" {
  type    = number
  default = 4
}
variable "max_throughput_units" {
  type    = number
  default = 20
}
variable "tags" {
  type    = map(string)
  default = {}
}
