# One user-assigned Managed Identity used by the processor for ALL auth.
resource "azurerm_user_assigned_identity" "mi" {
  name                = "${var.name_prefix}-mi"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}
