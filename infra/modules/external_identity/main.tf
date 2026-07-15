# One identity per external actor pyre needs to trust (a log sender, a CI
# publisher, ...), generalized over the two ways an actor OUTSIDE pyre's own
# processor Managed Identity can authenticate to Azure:
#   - it IS an Azure resource: it already has an identity -> mode =
#     "managed_identity". This module creates nothing; it's a passthrough
#     for that identity's object ID.
#   - it is NOT an Azure resource (a laptop, GitHub Actions, an ADO
#     Microsoft-hosted agent): it has nothing to attach an identity to ->
#     mode = "federated". This module provisions a user-assigned identity
#     plus an OIDC trust (Workload Identity Federation) so it can get Azure
#     AD tokens with no stored secret.
# Callers only ever consume the `principal_id` output; they never branch on
# mode themselves - see infra/main.tf.
resource "azurerm_user_assigned_identity" "this" {
  count               = var.mode == "federated" ? 1 : 0
  name                = "${var.name_prefix}-${var.name}-mi"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_federated_identity_credential" "this" {
  for_each            = var.mode == "federated" ? { for c in var.federated_credentials : c.name => c } : {}
  name                = each.value.name
  resource_group_name = var.resource_group_name
  parent_id           = azurerm_user_assigned_identity.this[0].id
  issuer              = each.value.issuer
  subject             = each.value.subject
  audience            = each.value.audience
}
