# Ingestion bus. One or more namespaces (from config/sources.yaml), each with one
# or more hubs. Every namespace: public access off, reached via private endpoint,
# Entra-only (no SAS). Partitions = the consuming Function App's parallelism ceiling.
locals {
  # Standard on both profiles: Basic supports neither private endpoints nor
  # auto-inflate, which this design requires.
  sku      = "Standard"
  capacity = var.cost_profile == "scale" ? var.throughput_units_scale : 1

  # Flatten namespaces -> hubs into one "<ns>/<hub>" map for a single for_each.
  ns_hubs = merge([for ns_name, ns in var.namespaces : {
    for hub_name, h in ns.hubs : "${ns_name}/${hub_name}" => {
      namespace  = ns_name, hub = hub_name
      partitions = h.partitions, retention_hours = h.retention_hours
    }
  }]...)

  # A representative created namespace for the uniform posture outputs below
  # (all created namespaces share cost_profile and allowlist).
  rep = values(azurerm_eventhub_namespace.ns)[0]
}

resource "azurerm_eventhub_namespace" "ns" {
  for_each                 = var.namespaces
  name                     = "${var.name_prefix}-${each.key}-ehns"
  location                 = var.location
  resource_group_name      = var.resource_group_name
  sku                      = local.sku
  capacity                 = local.capacity
  auto_inflate_enabled     = var.cost_profile == "scale"
  maximum_throughput_units = var.cost_profile == "scale" ? var.max_throughput_units : null
  # Always private: reached only via the private endpoint below, from inside the
  # VNet. Entra-only (no SAS). Deploy from a VNet-resident agent/VM.
  public_network_access_enabled = false
  local_authentication_enabled  = false
  tags                          = var.tags
}

resource "azurerm_eventhub" "hub" {
  for_each          = local.ns_hubs
  name              = each.value.hub
  namespace_id      = azurerm_eventhub_namespace.ns[each.value.namespace].id
  partition_count   = each.value.partitions
  message_retention = max(1, ceil(each.value.retention_hours / 24)) # config is hours; attribute is days
}

resource "azurerm_private_endpoint" "pe" {
  for_each            = var.namespaces
  name                = "${var.name_prefix}-${each.key}-ehns-pe"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.pe_subnet_id
  private_service_connection {
    name                           = "ehns"
    private_connection_resource_id = azurerm_eventhub_namespace.ns[each.key].id
    subresource_names              = ["namespace"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "ehns"
    private_dns_zone_ids = [var.dns_zone_id]
  }
  tags = var.tags
}

# RBAC: the processor receives on every created namespace; the log sender sends.
resource "azurerm_role_assignment" "receiver" {
  for_each             = var.namespaces
  scope                = azurerm_eventhub_namespace.ns[each.key].id
  role_definition_name = "Azure Event Hubs Data Receiver"
  principal_id         = var.processor_principal_id
}
resource "azurerm_role_assignment" "sender" {
  for_each             = var.sender_enabled ? var.namespaces : {}
  scope                = azurerm_eventhub_namespace.ns[each.key].id
  role_definition_name = "Azure Event Hubs Data Sender"
  principal_id         = var.sender_principal_id
}
