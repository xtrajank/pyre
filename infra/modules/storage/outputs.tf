output "account_name" { value = azurerm_storage_account.sa.name }
output "id" { value = azurerm_storage_account.sa.id }
output "blob_endpoint" { value = azurerm_storage_account.sa.primary_blob_endpoint }
