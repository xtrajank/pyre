variable "name_prefix" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }
variable "address_space" {
  type    = string
  default = "10.60.0.0/16"
}
variable "functions_subnet_prefix" {
  type    = string
  default = "10.60.1.0/24"
}
variable "pe_subnet_prefix" {
  type    = string
  default = "10.60.2.0/24"
}
variable "tags" {
  type    = map(string)
  default = {}
}
