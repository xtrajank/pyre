# Ingestion bus. Public network access disabled; reached via private endpoint.
# One namespace, one or more hubs (from config/sources.yaml). Partitions =
# parallelism ceiling for the consuming Function App.
locals {
  sku      = var.cost_profile == "scale" ? "Standard" : "Standard"
  capacity = var.cost_profile == "scale" ? var.throughput_units_scale : 1
}

resource "azurerm_eventhub_namespace" "ns" {
  name                          = "${var.name_prefix}-ehns"
  location                      = var.location
  resource_group_name           = var.resource_group_name
  sku                           = local.sku
  capacity                      = local.capacity
  auto_inflate_enabled          = var.cost_profile == "scale"
  maximum_throughput_units      = var.cost_profile == "scale" ? var.max_throughput_units : null
  public_network_access_enabled = false # reached only via the private endpoint below
  local_authentication_enabled  = false # force Entra/Managed Identity (no SAS keys)
  tags                          = var.tags
}

resource "azurerm_eventhub" "hub" {
  for_each          = var.hubs
  name              = each.key
  namespace_id      = azurerm_eventhub_namespace.ns.id
  partition_count   = each.value.partitions
  message_retention = each.value.retention_hours >= 24 ? 1 : 1
}

resource "azurerm_private_endpoint" "pe" {
  name                = "${var.name_prefix}-ehns-pe"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.pe_subnet_id
  private_service_connection {
    name                           = "ehns"
    private_connection_resource_id = azurerm_eventhub_namespace.ns.id
    subresource_names              = ["namespace"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "ehns"
    private_dns_zone_ids = [var.dns_zone_id]
  }
  tags = var.tags
}

# RBAC: processor identity receives; Cribl sender identity sends. No keys.
resource "azurerm_role_assignment" "receiver" {
  scope                = azurerm_eventhub_namespace.ns.id
  role_definition_name = "Azure Event Hubs Data Receiver"
  principal_id         = var.processor_principal_id
}
resource "azurerm_role_assignment" "sender" {
  count                = var.sender_principal_id == "" ? 0 : 1
  scope                = azurerm_eventhub_namespace.ns.id
  role_definition_name = "Azure Event Hubs Data Sender"
  principal_id         = var.sender_principal_id
}
