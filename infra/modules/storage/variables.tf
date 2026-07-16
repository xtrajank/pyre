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
variable "publisher_enabled" {
  type    = bool
  default = false
  # Whether publisher_principal_id names a real identity. Separate from the id
  # itself because that id is unknown at plan time when the publisher is
  # federated (Terraform creates it in the same apply), and a count argument
  # must be knowable during plan. The caller derives this from its own input
  # variables - see local.publisher_enabled in infra/main.tf.
}
variable "tags" {
  type    = map(string)
  default = {}
}
