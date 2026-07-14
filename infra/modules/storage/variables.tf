variable "name_prefix" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "pe_subnet_id" { type = string }
variable "dns_zone_id" { type = string }
variable "processor_principal_id" { type = string }
variable "publisher_principal_id" {
  type    = string
  default = ""
} # Azure Pipelines Workload Identity Federation service connection that runs `pyre publish`
variable "tags" {
  type    = map(string)
  default = {}
}
