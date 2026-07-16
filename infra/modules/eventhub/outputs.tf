# name -> FQDN, for the Function App's per-namespace trigger connections.
output "namespace_fqdns" {
  value = { for k, ns in azurerm_eventhub_namespace.ns : k => "${ns.name}.servicebus.windows.net" }
}
output "hub_names" { value = [for h in azurerm_eventhub.hub : h.name] }

# --- posture, asserted by infra/tests/plan.tftest.hcl -------------------------
# Uniform across created namespaces (shared cost_profile + allowlist), so these
# read from a representative one (locals.rep) or aggregate.
output "sku" { value = local.rep.sku }
output "capacity" { value = local.rep.capacity }
output "auto_inflate_enabled" { value = alltrue([for ns in azurerm_eventhub_namespace.ns : ns.auto_inflate_enabled]) }
# false only if EVERY namespace is private (anytrue -> any one public flips it).
output "public_network_access_enabled" { value = anytrue([for ns in azurerm_eventhub_namespace.ns : ns.public_network_access_enabled]) }
output "local_authentication_enabled" { value = anytrue([for ns in azurerm_eventhub_namespace.ns : ns.local_authentication_enabled]) }
