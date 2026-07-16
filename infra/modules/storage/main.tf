# Private storage: Event Hub checkpoints + deploy artifact bundle.
resource "azurerm_storage_account" "sa" {
  name                = replace("${var.name_prefix}stor", "-", "")
  location            = var.location
  resource_group_name = var.resource_group_name
  account_tier        = "Standard"
  # Checkpoints and deploy/detection bundles are all reconstructible from Event
  # Hubs and the DaC repo, so cross-region replication would be paid-for
  # durability nothing here needs.
  account_replication_type = "LRS"
  min_tls_version          = "TLS1_2"
  # Set explicitly rather than inherited. The provider currently defaults this
  # to true, but a security control that holds only because of a provider
  # default is one major version away from silently flipping - and it reads as
  # unspecified to anyone auditing the config.
  https_traffic_only_enabled = true
  # Always private: reached only via the private endpoint, from inside the VNet.
  # Entra-only (no access keys). Container creation / `pyre publish` run from a
  # VNet-resident deployer.
  public_network_access_enabled = false
  shared_access_key_enabled     = false
  tags                          = var.tags
}
resource "azurerm_storage_container" "checkpoints" {
  name                  = "checkpoints"
  storage_account_id    = azurerm_storage_account.sa.id
  container_access_type = "private"
}
resource "azurerm_storage_container" "bundle" {
  name                  = "bundle"
  storage_account_id    = azurerm_storage_account.sa.id
  container_access_type = "private"
}
# Detection bundles published from the DaC repo (`pyre publish`): versioned zips
# + a current.json pointer. Separate from the Flex deploy-package "bundle" above.
# The processor reads it via the account-scoped Blob Data role below; the CI
# publisher writes it via its own Workload Identity Federation service connection.
resource "azurerm_storage_container" "detections" {
  name                  = "detections"
  storage_account_id    = azurerm_storage_account.sa.id
  container_access_type = "private"
}
resource "azurerm_private_endpoint" "pe" {
  name                = "${var.name_prefix}-blob-pe"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.pe_subnet_id
  private_service_connection {
    name                           = "blob"
    private_connection_resource_id = azurerm_storage_account.sa.id
    subresource_names              = ["blob"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "blob"
    private_dns_zone_ids = [var.dns_zone_id]
  }
  tags = var.tags
}
resource "azurerm_role_assignment" "blob_data" {
  scope                = azurerm_storage_account.sa.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = var.processor_principal_id
}
# CI publisher (Azure Pipelines service connection) writes detection bundles + the pointer.
# Scoped to the detections container only - least privilege vs. the whole account.
resource "azurerm_role_assignment" "detections_publisher" {
  # Gated on a plan-time-known flag, not on the principal id itself: when the
  # publisher is federated, its principal_id is created in this same apply and
  # is therefore unknown at plan, which a count argument may not be.
  count                = var.publisher_enabled ? 1 : 0
  scope                = azurerm_storage_container.detections.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = var.publisher_principal_id
}
