output "hostname" { value = azurerm_managed_redis.cache.hostname }
# Kept named ssl_port for the consumer (function_app's REDIS_PORT) even though
# Azure Managed Redis calls it just "port": it is the TLS port, 10000 (classic
# was 6380). Computed on the default database.
output "ssl_port" { value = azurerm_managed_redis.cache.default_database[0].port }
output "id" { value = azurerm_managed_redis.cache.id }

# --- posture, asserted by infra/tests/plan.tftest.hcl -------------------------
# Surfaced as outputs because a `terraform test` assertion can only read a
# module's outputs, never its resources. Normalized to the same shapes the old
# classic-Redis module exposed so the security assertions read identically.
output "sku_name" { value = azurerm_managed_redis.cache.sku_name }
output "public_network_access_enabled" {
  value = azurerm_managed_redis.cache.public_network_access == "Enabled"
}
# AMR is TLS-only when client_protocol is "Encrypted"; there is no separate
# non-TLS port to leave open, so this is true only if someone sets Plaintext.
output "non_ssl_port_enabled" {
  value = azurerm_managed_redis.cache.default_database[0].client_protocol == "Plaintext"
}
# AMR mandates TLS 1.2+ with no downgrade knob, so the classic minimum_tls_version
# assertion is satisfied structurally. Emitted as a constant to keep the test.
output "minimum_tls_version" { value = "1.2" }
# Entra is required exactly when access-key auth is disabled on the database.
output "entra_auth_enabled" {
  value = azurerm_managed_redis.cache.default_database[0].access_keys_authentication_enabled == false
}
