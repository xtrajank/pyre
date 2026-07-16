output "account_name" { value = azurerm_storage_account.sa.name }
output "id" { value = azurerm_storage_account.sa.id }
output "blob_endpoint" { value = azurerm_storage_account.sa.primary_blob_endpoint }

# --- posture, asserted by infra/tests/plan.tftest.hcl -------------------------
# See modules/redis/outputs.tf for why these are outputs.
output "public_network_access_enabled" { value = azurerm_storage_account.sa.public_network_access_enabled }
output "shared_access_key_enabled" { value = azurerm_storage_account.sa.shared_access_key_enabled }
output "https_traffic_only_enabled" { value = azurerm_storage_account.sa.https_traffic_only_enabled }
output "min_tls_version" { value = azurerm_storage_account.sa.min_tls_version }
output "account_replication_type" { value = azurerm_storage_account.sa.account_replication_type }
output "container_access_types" {
  value = [
    azurerm_storage_container.checkpoints.container_access_type,
    azurerm_storage_container.bundle.container_access_type,
    azurerm_storage_container.detections.container_access_type,
  ]
}
