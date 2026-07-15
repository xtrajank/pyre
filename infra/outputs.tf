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
  value       = module.keyvault.name
  description = "Engine runtime secrets (Torq tokens, Cribl creds). Store them here: az keyvault secret set --vault-name <this> ..."
}
output "ci_key_vault_name" {
  value       = module.ci_keyvault.name
  description = "CI-only secrets (e.g. a cross-org DaC PAT). Separate from key_vault_name above - link an Azure Pipelines variable group to THIS vault, not the engine's."
}
output "publisher_client_id" {
  value       = module.publisher_identity.client_id
  description = "Set only when publisher.mode = \"federated\". Give this to your CI platform's OIDC login step (e.g. azure/login's client-id, or an ADO manual workload-identity-federation service connection) - null when publisher.mode = \"managed_identity\"."
}
output "log_sender_client_id" {
  value       = module.log_sender_identity.client_id
  description = "Set only when log_sender.mode = \"federated\". Null when log_sender.mode = \"managed_identity\"."
}
