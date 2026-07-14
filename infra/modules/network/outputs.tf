output "vnet_id" { value = azurerm_virtual_network.vnet.id }
output "functions_subnet_id" { value = azurerm_subnet.functions.id }
output "pe_subnet_id" { value = azurerm_subnet.private_endpoints.id }
output "dns_zone_ids" { value = { for k, z in azurerm_private_dns_zone.z : k => z.id } }
