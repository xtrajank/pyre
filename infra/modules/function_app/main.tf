# The processor. Flex Consumption, VNet-integrated, Managed Identity only.
# No public inbound: the Event Hub trigger pulls. App settings reference
# Event Hubs/Redis/Key Vault by identity, never by connection string.

locals {
  # Alert destinations -> app settings, generated rather than hardcoded per tool.
  #
  # These used to be a fixed MOCK_DEST_URL / TORQ_DEV_URL / TORQ_PROD_URL /
  # TORQ_*_TOKEN set, which baked one vendor and one dev/prod shape into the
  # infrastructure: an instance that used a plain webhook still carried Torq
  # settings, and a second Torq-like destination meant editing Terraform. The
  # engine never cared - it resolves whatever `url_env`/`token_env` name
  # config/destinations.yaml gives it - so the names are derived from the
  # destination's own name instead.
  #
  # A name is upper-cased and dashes become underscores, because an app setting
  # key must be an env var name: `torq_prod` -> DESTINATION_TORQ_PROD_URL.
  dest_key = { for name, d in var.destinations : name => upper(replace(name, "-", "_")) }

  dest_urls = {
    for name, d in var.destinations : "DESTINATION_${local.dest_key[name]}_URL" => d.url
  }

  # Only for destinations that declared a secret. A destination with no
  # token_secret gets NO token setting at all, so its adapter sees an unset env
  # var and fails closed - rather than a Key Vault reference to a secret nobody
  # created, which would resolve to a broken literal and fail at dispatch time
  # with a far worse error.
  dest_tokens = {
    for name, d in var.destinations :
    "DESTINATION_${local.dest_key[name]}_TOKEN" => "@Microsoft.KeyVault(SecretUri=${var.kv_uri}secrets/${d.token_secret}/)"
    if d.token_secret != ""
  }

  # One Managed-Identity trigger connection per namespace: the Event Hubs trigger
  # resolves connection="EVENTHUB_<NS>" from these EVENTHUB_<NS>__* settings.
  ns_key = { for name in keys(var.namespaces) : name => upper(replace(name, "-", "_")) }
  eh_conn = merge([for name, ns in var.namespaces : {
    "EVENTHUB_${local.ns_key[name]}__fullyQualifiedNamespace" = ns.fqdn
    "EVENTHUB_${local.ns_key[name]}__credential"              = "managedidentity"
    "EVENTHUB_${local.ns_key[name]}__clientId"                = var.identity_client_id
  }]...)

  # The engine reads this at cold start and registers one trigger per hub, each
  # bound to its namespace's connection and read with its namespace's shape.
  hubs_config = jsonencode([for h in var.hubs : {
    hub              = h.hub
    connection       = "EVENTHUB_${local.ns_key[h.namespace]}"
    log_type_field   = h.log_type_field
    event_time_field = h.event_time_field
    envelope         = h.envelope
  }])
}
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
  # Always private, no public inbound: the Event Hub trigger PULLS, and `func
  # publish` runs from inside the VNet. VNet-integrated to reach the private
  # services.
  public_network_access_enabled = false
  virtual_network_subnet_id     = var.functions_subnet_id

  identity {
    type         = "UserAssigned"
    identity_ids = [var.identity_id]
  }

  # Required block; runtime is set above. Batch size / concurrency tuning for the
  # Event Hub trigger lives in engine/host.json, not here.
  site_config {}

  app_settings = merge({
    PYRE_ENV = var.env
    # Every hub the processor consumes, with each hub's namespace connection and
    # shape. engine/function_app.py reads this at cold start and registers one
    # Event Hubs trigger per hub, so adding a source/hub/namespace to
    # config/sources.yaml grows this and the engine picks it up on its next
    # deploy - no engine code change.
    HUBS_CONFIG     = local.hubs_config
    REDIS_HOST      = var.redis_host
    REDIS_PORT      = tostring(var.redis_ssl_port)
    REDIS_USE_ENTRA = "true"
    # Detections come from the external DaC repo, published to Blob by CI and
    # hot-reloaded by the worker (config/detections.yaml bundle.mode: blob). The
    # worker reads the bundle via this identity's Blob Data role - no PAT here.
    BUNDLE_MODE              = "blob"
    BUNDLE_BLOB_ACCOUNT_URL  = var.bundle_blob_account_url
    REFRESH_INTERVAL_SECONDS = tostring(var.refresh_interval_seconds)
    STORM_LIMIT              = tostring(var.storm_limit)
    # Per-EVENT idempotency key TTL. Resident Redis keys ~= events/sec x this, so
    # it is the main Redis memory driver at volume. See root variables.tf.
    IDEMPOTENCY_TTL_SECONDS = tostring(var.idempotency_ttl_seconds)

    # Per-instance concurrency, pinned rather than left to the worker's defaults.
    # rule() evaluation is CPU-bound Python, so threads inside ONE process serialise
    # on the GIL: thread count alone buys concurrency only across the Redis/HTTP
    # waits. Real parallelism comes from PROCESSES. Pinning both makes throughput
    # per instance a number you chose (and can load-test) instead of one that
    # silently changes with the host's core count or a worker release.
    # NOTE: each process holds its OWN Processor -> its own bundle copy in memory
    # and its own Redis pool, so this trades memory (instance_memory_in_mb) for
    # CPU. Raise processes to use more cores; raise threads if profiling shows
    # workers parked on Redis/Cribl rather than on CPU.
    FUNCTIONS_WORKER_PROCESS_COUNT = tostring(var.worker_process_count)
    PYTHON_THREADPOOL_THREAD_COUNT = tostring(var.threads_per_worker)
    # Where an alert goes when its detection names no destination itself.
    # Explicit per instance: this replaced a hardcoded `env == "dev" ? mock :
    # torq_prod` in the engine, under which ANY env string that wasn't exactly
    # "dev" routed to production. A dev instance sets ["mock"] and declares no
    # production destination at all.
    DEFAULT_ROUTES = join(",", var.default_routes)
    # Batch <-> single-event knob. Overrides host.json's maxEventBatchSize at
    # runtime (the AzureFunctionsJobHost__ prefix maps to host.json). 1 = process
    # one event per invocation (lowest cost-efficiency, for low-volume/latency-
    # critical instances); 256 = batched (the scalable default). NOTE: this is a
    # CEILING, not a wait — small backlogs are still delivered immediately, so a
    # high value does NOT delay alerts at low volume. The processor handles any
    # size unchanged.
    "AzureFunctionsJobHost__extensions__eventHubs__maxEventBatchSize" = tostring(var.max_event_batch_size)
    SIGNALS_SINK_URL                                                  = var.signals_sink_url
    APPLICATIONINSIGHTS_CONNECTION_STRING                             = var.app_insights_conn
    },
    # One Managed-Identity trigger connection per namespace (EVENTHUB_<NS>__*).
    local.eh_conn,
    # Per-destination URL + token settings, derived from var.destinations. The
    # URL is not a secret and is a plain value; the token is always a Key Vault
    # reference into the ENGINE's vault (module.keyvault) - never the CI-only
    # vault, and never a literal.
    local.dest_urls,
    local.dest_tokens,
  )
  tags = var.tags
}
