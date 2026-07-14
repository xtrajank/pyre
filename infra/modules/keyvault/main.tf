# Secrets safe. Private endpoint, no public access, RBAC (not access policies).
# This module is instantiated TWICE (see infra/main.tf): one vault for the
# engine's runtime secrets (Torq tokens, Cribl creds - read by the processor
# Managed Identity), and a SEPARATE vault for CI-only secrets (read by the
# Azure Pipelines publisher service connection). Two vaults, not one with two
# roles, so a compromised identity on one side can never read the other
# side's secrets - the processor MI is never granted a role on the CI vault,
# and the publisher identity is never granted a role on the engine vault.
# purge_protection guards against accidental deletion.
data "azurerm_client_config" "current" {}
resource "azurerm_key_vault" "kv" {
  name                          = "${var.name_prefix}-${var.name_suffix}"
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
  name                = "${var.name_prefix}-${var.name_suffix}-pe"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.pe_subnet_id
  private_service_connection {
    name                           = var.name_suffix
    private_connection_resource_id = azurerm_key_vault.kv.id
    subresource_names              = ["vault"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = var.name_suffix
    private_dns_zone_ids = [var.dns_zone_id]
  }
  tags = var.tags
}
# One role assignment per granted reader - see reader_principal_ids for why
# this is a list instead of a single hardcoded identity.
resource "azurerm_role_assignment" "secrets_user" {
  for_each             = toset(var.reader_principal_ids)
  scope                = azurerm_key_vault.kv.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = each.value
}
