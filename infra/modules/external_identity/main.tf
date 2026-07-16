# One identity per external actor pyre trusts (a log sender, a CI publisher),
# over the two ways an actor outside the processor MI proves identity to Azure:
#   mode = "managed_identity" - it IS an Azure resource with its own identity;
#     this module is a passthrough for that object id, creating nothing.
#   mode = "federated"        - it is NOT (a laptop, GitHub Actions, an ADO
#     hosted agent); this provisions a user-assigned identity + an OIDC trust
#     (Workload Identity Federation) so it gets tokens with no stored secret.
# Callers consume only the principal_id output; they never branch on mode.
resource "azurerm_user_assigned_identity" "this" {
  count               = var.mode == "federated" ? 1 : 0
  name                = "${var.name_prefix}-${var.name}-mi"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "this" {
  for_each  = var.mode == "federated" ? { for c in var.federated_credentials : c.name => c } : {}
  name      = each.value.name
  parent_id = azurerm_user_assigned_identity.this[0].id
  issuer    = each.value.issuer
  subject   = each.value.subject
  audience  = each.value.audience
}
