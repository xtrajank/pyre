variable "name_prefix" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "pe_subnet_id" { type = string }
variable "dns_zone_id" { type = string }
variable "name_suffix" {
  type    = string
  default = "kv" # distinguishes multiple vault instances in one deployed instance, e.g. "kv" vs "ci-kv"
}
variable "reader_principal_ids" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Identities granted "Key Vault Secrets User" on THIS vault only. Least
    privilege by construction: the engine's runtime vault grants only the
    processor Managed Identity; a separate CI vault (see infra/main.tf) grants
    only the publisher service connection. Neither identity is ever granted
    the other vault's role, so a compromise of one can't read the other's
    secrets.
  EOT
}
variable "tags" {
  type    = map(string)
  default = {}
}
