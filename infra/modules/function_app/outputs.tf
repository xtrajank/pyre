output "name" { value = azurerm_function_app_flex_consumption.app.name }
output "id" { value = azurerm_function_app_flex_consumption.app.id }

# --- posture, asserted by infra/tests/plan.tftest.hcl -------------------------
# See modules/redis/outputs.tf for why these are outputs.
output "sku_name" { value = azurerm_service_plan.plan.sku_name }
output "maximum_instance_count" { value = azurerm_function_app_flex_consumption.app.maximum_instance_count }
output "instance_memory_in_mb" { value = azurerm_function_app_flex_consumption.app.instance_memory_in_mb }
output "public_network_access_enabled" { value = azurerm_function_app_flex_consumption.app.public_network_access_enabled }
output "runtime_version" { value = azurerm_function_app_flex_consumption.app.runtime_version }

# The hubs the Event Hubs triggers bind to. Asserted against the hubs
# config/sources.yaml actually creates: both sides are just strings, so a
# mismatch plans and applies green and then silently evaluates nothing.
output "eventhub_names" { value = [for h in var.hubs : h.hub] }

# Every app setting, so a test can assert no secret is inlined as a literal.
output "app_settings" { value = azurerm_function_app_flex_consumption.app.app_settings }

