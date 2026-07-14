variable "name_prefix" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "cost_profile" { type = string }
variable "pe_subnet_id" { type = string }
variable "dns_zone_id" { type = string }
variable "processor_principal_id" { type = string } # MI granted Redis data access (Entra auth)
variable "tags" {
  type    = map(string)
  default = {}
}
