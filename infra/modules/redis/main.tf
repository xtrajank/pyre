# Dedup/threshold/unique/storm state store. Private endpoint, no public access,
# Entra auth (no access keys). test=Basic C0; scale=Premium (clustered).
locals {
  is_scale = var.cost_profile == "scale"
}
resource "azurerm_redis_cache" "cache" {
  name                          = "${var.name_prefix}-redis"
  location                      = var.location
  resource_group_name           = var.resource_group_name
  capacity                      = local.is_scale ? 1 : 0
  family                        = local.is_scale ? "P" : "C"
  sku_name                      = local.is_scale ? "Premium" : "Basic"
  non_ssl_port_enabled          = false
  minimum_tls_version           = "1.2"
  public_network_access_enabled = false # reached only via the private endpoint below
  redis_configuration {
    active_directory_authentication_enabled = true # Entra auth, no keys
  }
  tags = var.tags
}

# Grant the processor's Managed Identity data-plane access to Redis. Without this
# the engine can authenticate to Event Hub/Blob but NOT run dedup against Redis.
# The design uses Entra auth, never access keys.
resource "azurerm_redis_cache_access_policy_assignment" "processor" {
  name               = "processor-contributor"
  redis_cache_id     = azurerm_redis_cache.cache.id
  access_policy_name = "Data Contributor"
  object_id          = var.processor_principal_id
  object_id_alias    = "processor-mi"
}

resource "azurerm_private_endpoint" "pe" {
  name                = "${var.name_prefix}-redis-pe"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.pe_subnet_id
  private_service_connection {
    name                           = "redis"
    private_connection_resource_id = azurerm_redis_cache.cache.id
    subresource_names              = ["redisCache"]
    is_manual_connection           = false
  }
  private_dns_zone_group {
    name                 = "redis"
    private_dns_zone_ids = [var.dns_zone_id]
  }
  tags = var.tags
}
