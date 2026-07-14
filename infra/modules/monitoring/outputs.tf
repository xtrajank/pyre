output "workspace_id" { value = azurerm_log_analytics_workspace.law.id }
output "app_insights_conn" {
  value     = azurerm_application_insights.ai.connection_string
  sensitive = true
}
