# Private network foundation. No public IPs. Function App integrates into a
# delegated subnet; all PaaS reached via private endpoints resolved by the
# private DNS zones created here.
resource "azurerm_virtual_network" "vnet" {
  name                = "${var.name_prefix}-vnet"
  location            = var.location
  resource_group_name = var.resource_group_name
  address_space       = [var.address_space]
  tags                = var.tags
}

resource "azurerm_subnet" "functions" {
  name                 = "snet-functions"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = [var.functions_subnet_prefix]
  delegation {
    name = "func-delegation"
    service_delegation { name = "Microsoft.App/environments" }
  }
}

resource "azurerm_subnet" "private_endpoints" {
  name                              = "snet-pe"
  resource_group_name               = var.resource_group_name
  virtual_network_name              = azurerm_virtual_network.vnet.name
  address_prefixes                  = [var.pe_subnet_prefix]
  private_endpoint_network_policies = "Enabled"
}

# Private DNS zones so private endpoints resolve to private IPs.
locals {
  zones = {
    eventhub = "privatelink.servicebus.windows.net"
    redis    = "privatelink.redis.cache.windows.net"
    keyvault = "privatelink.vaultcore.azure.net"
    blob     = "privatelink.blob.core.windows.net"
  }
}
resource "azurerm_private_dns_zone" "z" {
  for_each            = local.zones
  name                = each.value
  resource_group_name = var.resource_group_name
  tags                = var.tags
}
resource "azurerm_private_dns_zone_virtual_network_link" "link" {
  for_each              = local.zones
  name                  = "${each.key}-link"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.z[each.key].name
  virtual_network_id    = azurerm_virtual_network.vnet.id
}
