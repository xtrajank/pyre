output "namespace_name" { value = azurerm_eventhub_namespace.ns.name }
output "namespace_fqdn" { value = "${azurerm_eventhub_namespace.ns.name}.servicebus.windows.net" }
output "hub_names" { value = [for h in azurerm_eventhub.hub : h.name] }
