# Private storage: Event Hub checkpoints + deploy artifact bundle.
resource "azurerm_storage_account" "sa" {
  name                          = replace("${var.name_prefix}stor", "-", "")
  location                      = var.location
  resource_group_name           = var.resource_group_name
  account_tier                  = "Standard"
  account_replication_type      = "LRS"
  min_tls_version               = "TLS1_2"
  public_network_access_enabled = false # reached only via the private endpoint below
  shared_access_key_enabled     = false # force Entra/Managed Identity (no access keys)
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
# publisher writes it via its own OIDC service principal.
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
# CI publisher (GitHub Actions OIDC SP) writes detection bundles + the pointer.
# Scoped to the detections container only - least privilege vs. the whole account.
resource "azurerm_role_assignment" "detections_publisher" {
  count                = var.publisher_principal_id == "" ? 0 : 1
  scope                = azurerm_storage_container.detections.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = var.publisher_principal_id
}
