# Dedup/threshold/unique/storm state store. Private endpoint, no public access,
# Entra auth (no access keys).
#
# Uses Azure Managed Redis (Microsoft.Cache/redisEnterprise), NOT the classic
# Azure Cache for Redis (Microsoft.Cache/Redis): the classic service is retiring
# and Azure now rejects new classic caches outright ("Azure Cache for Redis is
# retiring, create Azure Managed Redis instance instead"). The engine talks plain
# RESP over TLS with an Entra token, so the switch is transparent to it — but the
# TLS port is 10000 (not the classic 6380) and the private DNS zone is
# redis.azure.net (not redis.cache.windows.net); both are handled here and in the
# network module.
locals {
  is_scale = var.cost_profile == "scale"
  # test  -> Balanced_B0: the smallest/cheapest Managed Redis SKU. A same-day lab
  #          costs cents/hour; tear it down (tools/LAB.md Part 11) and it stops.
  # scale -> Balanced_B5: a modest production floor that REPLACES the old Premium
  #          P1. Size this against your real dedup memory model (idempotency TTL x
  #          events/sec — see root variables.tf) before trusting it for prod.
  sku = local.is_scale ? "Balanced_B5" : "Balanced_B0"
}

resource "azurerm_managed_redis" "cache" {
  name                = "${var.name_prefix}-redis"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku_name            = local.sku
  # Reached only via the private endpoint below. AMR models this as a string, not
  # a bool (the module output normalizes it back to a bool for the tests).
  public_network_access = "Disabled"
  # Explicit, not left to the provider default: the lab's Balanced_B0 is a
  # single-node dev SKU (HA off — cheapest), prod gets zone-redundant HA. Setting
  # it removes a guess about what the default would bill or whether B0 accepts it.
  high_availability_enabled = local.is_scale

  default_database {
    # Entra auth only — no access keys, matching the classic module's posture.
    # The processor MI is granted data access via the assignment below.
    access_keys_authentication_enabled = false
    # TLS-only. "Plaintext" would expose an unencrypted port; never here.
    client_protocol = "Encrypted"
    # EnterpriseCluster presents ONE endpoint with no cross-slot MOVED redirects,
    # so the engine's single-node redis-py client (and its EVALSHA Lua scripts)
    # work unchanged at any SKU size. OSSCluster would break that client the
    # moment scale adds a second shard.
    clustering_policy = "EnterpriseCluster"
    # Every key the engine writes carries a TTL (idempotency, dedup/threshold
    # windows, storm counters), so evict only expiring keys under pressure.
    eviction_policy = "VolatileLRU"
  }

  tags = var.tags
}

# Grant the processor's Managed Identity data-plane access to Redis. Without this
# the engine can authenticate to Event Hub/Blob but NOT run dedup against Redis.
# The design uses Entra auth, never access keys.
resource "azurerm_managed_redis_access_policy_assignment" "processor" {
  managed_redis_id = azurerm_managed_redis.cache.id
  object_id        = var.processor_principal_id
}

resource "azurerm_private_endpoint" "pe" {
  name                = "${var.name_prefix}-redis-pe"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.pe_subnet_id
  private_service_connection {
    name                           = "redis"
    private_connection_resource_id = azurerm_managed_redis.cache.id
    # redisEnterprise, not the classic "redisCache" subresource.
    subresource_names    = ["redisEnterprise"]
    is_manual_connection = false
  }
  private_dns_zone_group {
    name                 = "redis"
    private_dns_zone_ids = [var.dns_zone_id]
  }
  tags = var.tags
}
