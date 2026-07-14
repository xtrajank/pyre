# Handy values after `terraform apply` — used by the post-deploy steps
# (deploy the engine, publish detections, point Cribl at the hub).
output "function_app_name" {
  value       = "${var.name_prefix}-proc"
  description = "Name of the Function App to `func azure functionapp publish`."
}
output "eventhub_namespace_fqdn" {
  value       = module.eventhub.namespace_fqdn
  description = "Point Cribl (and python_shipper.py) at this, hub `logs-in`."
}
output "storage_account_name" {
  value       = module.storage.account_name
  description = "Blob account. BUNDLE_BLOB_ACCOUNT_URL = https://<this>.blob.core.windows.net"
}
output "blob_endpoint" {
  value = module.storage.blob_endpoint
}
output "log_analytics_workspace" {
  value       = "${var.name_prefix}-law"
  description = "Where the engine's logs/metrics land (portal -> this -> Logs)."
}
output "key_vault_name" {
  value       = "${var.name_prefix}-kv"
  description = "Store the Torq token here: az keyvault secret set --vault-name <this> ..."
}
