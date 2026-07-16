# A Key Vault, secrets-safe: private endpoint, no public access, RBAC (not access
# policies). Instantiated twice (infra/main.tf) - engine-runtime secrets and
# CI-only secrets - with disjoint readers, so a compromise of one identity cannot
# read the other vault's secrets.
data "azurerm_client_config" "current" {}
resource "azurerm_key_vault" "kv" {
  name                       = "${var.name_prefix}-${var.name_suffix}"
  location                   = var.location
  resource_group_name        = var.resource_group_name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  # Prod locks the vault against permanent deletion (and its name for 90 days).
  # A dev instance turns this off so it can be destroyed and rebuilt under the
  # same name_prefix - otherwise every rebuild hits "VaultAlreadyExists" on a
  # soft-deleted, purge-protected remnant. Retention is the Azure minimum (7)
  # when unprotected so remnants clear fast, the 90-day maximum when protected.
  purge_protection_enabled      = var.purge_protection_enabled
  soft_delete_retention_days    = var.purge_protection_enabled ? 90 : 7
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
# count, not for_each: reader ids are Managed Identity object IDs created in this
# same apply (values unknown at plan), and for_each needs known keys. A list's
# LENGTH is known, so count plans cleanly. Tradeoff: positional - reordering
# re-creates assignments; callers pass a fixed single-element list, so it doesn't.
resource "azurerm_role_assignment" "secrets_user" {
  count                = length(var.reader_principal_ids)
  scope                = azurerm_key_vault.kv.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = var.reader_principal_ids[count.index]
}
