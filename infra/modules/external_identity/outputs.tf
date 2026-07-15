output "principal_id" {
  value       = var.mode == "managed_identity" ? var.principal_id : (length(azurerm_user_assigned_identity.this) > 0 ? azurerm_user_assigned_identity.this[0].principal_id : "")
  description = "Object ID to grant roles to. The caller's own identity when mode = \"managed_identity\"; the identity this module created when mode = \"federated\"."
}

output "client_id" {
  value       = length(azurerm_user_assigned_identity.this) > 0 ? azurerm_user_assigned_identity.this[0].client_id : null
  description = "Set only when mode = \"federated\" (the identity Terraform created here). Feed this to the CI platform's OIDC login step (e.g. azure/login's client-id input, or an ADO manual workload-identity-federation service connection). Null when mode = \"managed_identity\" - the actor already knows its own client ID."
}

output "tenant_id" {
  value       = length(azurerm_user_assigned_identity.this) > 0 ? azurerm_user_assigned_identity.this[0].tenant_id : null
  description = "Set only when mode = \"federated\". Null when mode = \"managed_identity\"."
}
