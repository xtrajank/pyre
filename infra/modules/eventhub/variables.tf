variable "name_prefix" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "cost_profile" { type = string }
variable "hubs" { type = map(object({ partitions = number, retention_hours = number })) }
variable "pe_subnet_id" { type = string }
variable "dns_zone_id" { type = string }
variable "processor_principal_id" { type = string }
variable "sender_principal_id" {
  type    = string
  default = ""
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
