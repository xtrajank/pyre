# Secrets safe. Private endpoint, no public access, RBAC (not access policies).
# Holds the Torq token and any Cribl credentials; the processor reads them by
# reference (never inlined). purge_protection guards against accidental deletion.
data "azurerm_client_config" "current" {}
resource "azurerm_key_vault" "kv" {
  name                          = "${var.name_prefix}-kv"
  location                      = var.location
  resource_group_name           = var.resource_group_name
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = "standard"
  rbac_authorization_enabled    = true
  purge_protection_enabled      = true
  public_network_access_enabled = false # reached only via the private endpoint below
  tags                          = var.tags
}
resource "azurerm_private_endpoint" "pe" {
  name                = "${var.name_prefix}-kv-pe"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.pe_subnet_id
  private_service_connection {
    name                           = "kv"
    private_connection_resource_id = azurerm_key_vault.kv.id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "kv"
    private_dns_zone_ids = [var.dns_zone_id]
  }
  tags = var.tags
}
# Processor identity may read secrets (Torq token, Cribl creds).
resource "azurerm_role_assignment" "secrets_user" {
  scope                = azurerm_key_vault.kv.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = var.processor_principal_id
}
