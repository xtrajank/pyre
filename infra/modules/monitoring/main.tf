# Observability. Log Analytics is the query store; Application Insights feeds the
# Function App's logs/metrics/traces into it. This is where you watch the engine
# run and debug it (see docs/PRODUCTION.md § Monitoring). 30-day retention.
resource "azurerm_log_analytics_workspace" "law" {
  name                = "${var.name_prefix}-law"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = var.tags
}
resource "azurerm_application_insights" "ai" {
  name                = "${var.name_prefix}-ai"
  location            = var.location
  resource_group_name = var.resource_group_name
  workspace_id        = azurerm_log_analytics_workspace.law.id
  application_type    = "other"
  tags                = var.tags
}
