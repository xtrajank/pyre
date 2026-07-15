# The pyre composition — ONE definition of the whole system. Deploy it as many
# times as you like (dev, prod, per-team, per-region); each deployment is an
# "instance" selected by a .tfvars file (see infra/envs/*.tfvars.example) and its
# own state key (see backend.tf). dev is simply prod with cheaper values.
#
# Modules are wired only through each other's outputs, so any one can be replaced
# without touching the rest. Provider/backend live in providers.tf / backend.tf.

locals {
  tags = {
    system = var.name_prefix
    env    = var.env
    owner  = var.owner
  }
  # Build the Event Hub set from config/sources.yaml (shared by every instance).
  hubs = { for s in yamldecode(file("${path.module}/../config/sources.yaml")).sources :
  s.hub => { partitions = s.partitions, retention_hours = s.retention_hours }... }
  # collapse duplicate hub keys, taking the max partitions/retention requested
  hub_map = { for k, v in local.hubs : k => { partitions = max([for x in v : x.partitions]...),
  retention_hours = max([for x in v : x.retention_hours]...) } }
}

module "network" {
  source                  = "./modules/network"
  name_prefix             = var.name_prefix
  location                = var.location
  resource_group_name     = var.resource_group_name
  address_space           = var.address_space
  pe_subnet_prefix        = var.pe_subnet_prefix
  functions_subnet_prefix = var.functions_subnet_prefix
  tags                    = local.tags
}

module "identity" {
  source              = "./modules/identity"
  name_prefix         = var.name_prefix
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = local.tags
}

module "monitoring" {
  source              = "./modules/monitoring"
  name_prefix         = var.name_prefix
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = local.tags
}

# The two external actors pyre trusts, generalized over how each proves its
# identity to Azure (an Azure resource's own Managed Identity, or an OIDC
# Workload Identity Federation trust for software outside Azure) - see
# infra/modules/external_identity and the `log_sender`/`publisher` variables.
module "log_sender_identity" {
  source                = "./modules/external_identity"
  name_prefix            = var.name_prefix
  location               = var.location
  resource_group_name    = var.resource_group_name
  name                   = "sender"
  mode                   = var.log_sender.mode
  principal_id           = var.log_sender.principal_id
  federated_credentials  = var.log_sender.federated_credentials
  tags                   = local.tags
}

module "publisher_identity" {
  source                = "./modules/external_identity"
  name_prefix            = var.name_prefix
  location               = var.location
  resource_group_name    = var.resource_group_name
  name                   = "publisher"
  mode                   = var.publisher.mode
  principal_id           = var.publisher.principal_id
  federated_credentials  = var.publisher.federated_credentials
  tags                   = local.tags
}

module "storage" {
  source                 = "./modules/storage"
  name_prefix            = var.name_prefix
  location               = var.location
  resource_group_name    = var.resource_group_name
  pe_subnet_id           = module.network.pe_subnet_id
  dns_zone_id            = module.network.dns_zone_ids["blob"]
  processor_principal_id = module.identity.principal_id
  publisher_principal_id = module.publisher_identity.principal_id
  tags                   = local.tags
}

module "keyvault" {
  source               = "./modules/keyvault"
  name_prefix          = var.name_prefix
  location             = var.location
  resource_group_name  = var.resource_group_name
  pe_subnet_id         = module.network.pe_subnet_id
  dns_zone_id          = module.network.dns_zone_ids["keyvault"]
  reader_principal_ids = [module.identity.principal_id] # engine runtime secrets: Torq tokens, log-sender creds
  tags                 = local.tags
}

# A SEPARATE vault for CI-only secrets (e.g. a cross-org DaC PAT, if the DaC
# repo ever isn't reachable via the pipeline's own OAuth token - see
# .azure-pipelines/publish-detections.yml). Only the publisher identity is
# granted a role here; it never touches module.keyvault above, and the
# processor Managed Identity never touches this one. See infra/README.md.
module "ci_keyvault" {
  source               = "./modules/keyvault"
  name_prefix          = var.name_prefix
  location             = var.location
  resource_group_name  = var.resource_group_name
  pe_subnet_id         = module.network.pe_subnet_id
  dns_zone_id          = module.network.dns_zone_ids["keyvault"]
  name_suffix          = "ci-kv"
  reader_principal_ids = module.publisher_identity.principal_id == "" ? [] : [module.publisher_identity.principal_id]
  tags                 = local.tags
}

module "redis" {
  source                 = "./modules/redis"
  name_prefix            = var.name_prefix
  location               = var.location
  resource_group_name    = var.resource_group_name
  cost_profile           = var.cost_profile
  pe_subnet_id           = module.network.pe_subnet_id
  dns_zone_id            = module.network.dns_zone_ids["redis"]
  processor_principal_id = module.identity.principal_id
  tags                   = local.tags
}

module "eventhub" {
  source                 = "./modules/eventhub"
  name_prefix            = var.name_prefix
  location               = var.location
  resource_group_name    = var.resource_group_name
  cost_profile           = var.cost_profile
  hubs                   = local.hub_map
  pe_subnet_id           = module.network.pe_subnet_id
  dns_zone_id            = module.network.dns_zone_ids["eventhub"]
  throughput_units_scale = var.throughput_units_floor
  max_throughput_units   = var.max_throughput_units
  processor_principal_id = module.identity.principal_id
  sender_principal_id    = module.log_sender_identity.principal_id
  tags                   = local.tags
}

module "function_app" {
  source                    = "./modules/function_app"
  name_prefix               = var.name_prefix
  location                  = var.location
  resource_group_name       = var.resource_group_name
  cost_profile              = var.cost_profile
  env                       = var.env
  identity_id               = module.identity.id
  identity_client_id        = module.identity.client_id
  functions_subnet_id       = module.network.functions_subnet_id
  deploy_container_endpoint = "https://${module.storage.account_name}.blob.core.windows.net/bundle"
  bundle_blob_account_url   = module.storage.blob_endpoint
  refresh_interval_seconds  = var.refresh_interval_seconds
  eventhub_name             = "logs-in"
  eventhub_fqdn             = module.eventhub.namespace_fqdn
  redis_host                = module.redis.hostname
  redis_ssl_port            = module.redis.ssl_port
  kv_uri                    = module.keyvault.uri
  app_insights_conn         = module.monitoring.app_insights_conn
  storm_limit               = var.storm_limit
  log_type_field            = var.log_type_field
  event_time_field          = var.event_time_field
  signals_sink_url          = var.signals_sink_url
  mock_dest_url             = var.mock_dest_url
  torq_dev_url              = var.torq_dev_url
  torq_prod_url             = var.torq_prod_url
  max_event_batch_size      = var.max_event_batch_size
  tags                      = local.tags
}
