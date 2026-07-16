# The processor. Flex Consumption, VNet-integrated, Managed Identity only.
# No public inbound: the Event Hub trigger pulls. App settings reference
# Event Hubs/Redis/Key Vault by identity, never by connection string.
resource "azurerm_service_plan" "plan" {
  name                = "${var.name_prefix}-flex-plan"
  location            = var.location
  resource_group_name = var.resource_group_name
  os_type             = "Linux"
  sku_name            = "FC1" # Flex Consumption
  tags                = var.tags
}

resource "azurerm_function_app_flex_consumption" "app" {
  name                              = "${var.name_prefix}-proc"
  location                          = var.location
  resource_group_name               = var.resource_group_name
  service_plan_id                   = azurerm_service_plan.plan.id
  storage_container_type            = "blobContainer"
  storage_container_endpoint        = var.deploy_container_endpoint
  storage_authentication_type       = "UserAssignedIdentity"
  storage_user_assigned_identity_id = var.identity_id
  runtime_name                      = "python"
  runtime_version                   = "3.11"
  maximum_instance_count            = var.cost_profile == "scale" ? 1000 : 40
  instance_memory_in_mb             = 2048
  public_network_access_enabled     = false                   # no public inbound; the Event Hub trigger pulls
  virtual_network_subnet_id         = var.functions_subnet_id # VNet-integrated to reach the private services

  identity {
    type         = "UserAssigned"
    identity_ids = [var.identity_id]
  }

  # Required block; runtime is set above. Batch size / concurrency tuning for the
  # Event Hub trigger lives in engine/host.json, not here.
  site_config {}

  app_settings = {
    PYRE_ENV      = var.env
    EVENTHUB_NAME = var.eventhub_name
    # Identity-based Event Hub trigger connection (no secret):
    "EVENTHUB_CONNECTION__fullyQualifiedNamespace" = var.eventhub_fqdn
    "EVENTHUB_CONNECTION__credential"              = "managedidentity"
    "EVENTHUB_CONNECTION__clientId"                = var.identity_client_id
    REDIS_HOST                                     = var.redis_host
    REDIS_PORT                                     = tostring(var.redis_ssl_port)
    REDIS_USE_ENTRA                                = "true"
    # Detections come from the external DaC repo, published to Blob by CI and
    # hot-reloaded by the worker (config/detections.yaml bundle.mode: blob). The
    # worker reads the bundle via this identity's Blob Data role - no PAT here.
    BUNDLE_MODE              = "blob"
    BUNDLE_BLOB_ACCOUNT_URL  = var.bundle_blob_account_url
    REFRESH_INTERVAL_SECONDS = tostring(var.refresh_interval_seconds)
    STORM_LIMIT              = tostring(var.storm_limit)
    # Which event field routes to detections and which carries the event's own
    # timestamp - configurable per feed (default matches Cribl's own field
    # names, not Panther's p_ prefix convention). See var.log_type_field.
    LOG_TYPE_FIELD   = var.log_type_field
    EVENT_TIME_FIELD = var.event_time_field
    MOCK_DEST_URL    = var.mock_dest_url # test-lab alert sink; empty in prod
    # Batch <-> single-event knob. Overrides host.json's maxEventBatchSize at
    # runtime (the AzureFunctionsJobHost__ prefix maps to host.json). 1 = process
    # one event per invocation (lowest cost-efficiency, for low-volume/latency-
    # critical instances); 256 = batched (the scalable default). NOTE: this is a
    # CEILING, not a wait — small backlogs are still delivered immediately, so a
    # high value does NOT delay alerts at low volume. The processor handles any
    # size unchanged.
    "AzureFunctionsJobHost__extensions__eventHubs__maxEventBatchSize" = tostring(var.max_event_batch_size)
    # Torq destinations (config/destinations.yaml torq_dev/torq_prod): the URL
    # isn't a secret and is a plain variable; the token is, via a Key Vault
    # reference - this is the engine's runtime vault (module.keyvault), never
    # the CI-only vault.
    TORQ_DEV_URL                          = var.torq_dev_url
    TORQ_DEV_TOKEN                        = "@Microsoft.KeyVault(SecretUri=${var.kv_uri}secrets/torq-dev-token/)"
    TORQ_PROD_URL                         = var.torq_prod_url
    TORQ_PROD_TOKEN                       = "@Microsoft.KeyVault(SecretUri=${var.kv_uri}secrets/torq-prod-token/)"
    SIGNALS_SINK_URL                      = var.signals_sink_url
    APPLICATIONINSIGHTS_CONNECTION_STRING = var.app_insights_conn
  }
  tags = var.tags
}
