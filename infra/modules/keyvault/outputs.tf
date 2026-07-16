output "id" { value = azurerm_key_vault.kv.id }
output "uri" { value = azurerm_key_vault.kv.vault_uri }
output "name" { value = azurerm_key_vault.kv.name }

# --- posture, asserted by infra/tests/plan.tftest.hcl -------------------------
# See modules/redis/outputs.tf for why these are outputs.
output "public_network_access_enabled" { value = azurerm_key_vault.kv.public_network_access_enabled }
output "rbac_authorization_enabled" { value = azurerm_key_vault.kv.rbac_authorization_enabled }
output "purge_protection_enabled" { value = azurerm_key_vault.kv.purge_protection_enabled }
# Who this vault grants "Key Vault Secrets User" to. The engine vault and the
# CI vault must never share a reader - that separation is the whole reason the
# module is instantiated twice.
output "reader_principal_ids" { value = var.reader_principal_ids }
