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
  # Log sources, grouped by Event Hub namespace (config/sources.yaml). One
  # namespace = one feed with one shape; add/remove a namespace, hub, or source
  # by editing that file.
  namespaces = yamldecode(file("${path.module}/../config/sources.yaml")).namespaces

  # Namespaces this instance CREATES vs binds to (create: false).
  create_ns   = { for ns in local.namespaces : ns.name => ns if try(ns.create, true) }
  existing_ns = { for ns in local.namespaces : ns.name => ns if !try(ns.create, true) }

  # Created namespaces -> hub sizing, for the eventhub module.
  create_ns_map = { for name, ns in local.create_ns : name => {
    hubs = { for h in ns.hubs : h.name => { partitions = h.partitions, retention_hours = h.retention_hours } }
  } }

  # Every hub across every namespace, with its namespace's shape, for the
  # function app. The processor consumes ALL of them (one trigger each): a hub
  # nobody consumes ingests at full rate and is evaluated by nothing, silently.
  all_hubs = flatten([for ns in local.namespaces : [
    for h in ns.hubs : {
      hub              = h.name
      namespace        = ns.name
      log_type_field   = try(ns.shape.log_type_field, "dataset")
      event_time_field = try(ns.shape.event_time_field, "_time")
      envelope         = try(ns.shape.envelope, "")
    }
  ]])

  # namespace -> FQDN: created (from the module) + bound (from the data source).
  namespace_fqdns = merge(
    module.eventhub.namespace_fqdns,
    { for name, d in data.azurerm_eventhub_namespace.existing : name => "${d.name}.servicebus.windows.net" },
  )

  # Per-namespace catch-all hub(s), for the tftest guardrail (exactly one each).
  ns_default_hubs = { for ns in local.namespaces : ns.name => [for h in ns.hubs : h.name if try(h.default, false)] }

  # Is each external actor configured at all? Derived from input VARIABLES only,
  # never from a module's computed principal_id (unknown at plan for a federated
  # identity created in the same apply, which a count/for_each key may not be).
  log_sender_enabled = var.log_sender.mode == "federated" ? length(var.log_sender.federated_credentials) > 0 : var.log_sender.principal_id != ""
  publisher_enabled  = var.publisher.mode == "federated" ? length(var.publisher.federated_credentials) > 0 : var.publisher.principal_id != ""
}

# Governance: who may change this instance. Each admin principal (a user, group,
# or service-principal OBJECT ID - in the company these already exist, so you map
# them by id) is granted, on THIS resource group only:
#   Contributor                 - create/modify the resources
#   User Access Administrator   - assign the RBAC this composition itself creates
# Add a person by adding them to the Entra group you list here - no Terraform run.
# The FIRST apply (which creates these assignments) must be run by an Owner /
# User Access Administrator; thereafter group membership governs access.
data "azurerm_resource_group" "this" {
  name = var.resource_group_name
}
resource "azurerm_role_assignment" "admin_contributor" {
  for_each             = toset(var.admin_principal_ids)
  scope                = data.azurerm_resource_group.this.id
  role_definition_name = "Contributor"
  principal_id         = each.value
}
resource "azurerm_role_assignment" "admin_user_access" {
  for_each             = toset(var.admin_principal_ids)
  scope                = data.azurerm_resource_group.this.id
  role_definition_name = "User Access Administrator"
  principal_id         = each.value
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
  name_prefix           = var.name_prefix
  location              = var.location
  resource_group_name   = var.resource_group_name
  name                  = "sender"
  mode                  = var.log_sender.mode
  principal_id          = var.log_sender.principal_id
  federated_credentials = var.log_sender.federated_credentials
  tags                  = local.tags
}

module "publisher_identity" {
  source                = "./modules/external_identity"
  name_prefix           = var.name_prefix
  location              = var.location
  resource_group_name   = var.resource_group_name
  name                  = "publisher"
  mode                  = var.publisher.mode
  principal_id          = var.publisher.principal_id
  federated_credentials = var.publisher.federated_credentials
  tags                  = local.tags
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
  publisher_enabled      = local.publisher_enabled
  tags                   = local.tags
}

module "keyvault" {
  source                   = "./modules/keyvault"
  name_prefix              = var.name_prefix
  location                 = var.location
  resource_group_name      = var.resource_group_name
  pe_subnet_id             = module.network.pe_subnet_id
  dns_zone_id              = module.network.dns_zone_ids["keyvault"]
  reader_principal_ids     = [module.identity.principal_id] # engine runtime secrets: Torq tokens, log-sender creds
  purge_protection_enabled = var.key_vault_purge_protection
  tags                     = local.tags
}

# A SEPARATE vault for CI-only secrets (e.g. a cross-org DaC PAT, if the DaC
# repo ever isn't reachable via the pipeline's own OAuth token - see
# .azure-pipelines/publish-detections.yml). Only the publisher identity is
# granted a role here; it never touches module.keyvault above, and the
# processor Managed Identity never touches this one. See infra/README.md.
module "ci_keyvault" {
  source                   = "./modules/keyvault"
  name_prefix              = var.name_prefix
  location                 = var.location
  resource_group_name      = var.resource_group_name
  pe_subnet_id             = module.network.pe_subnet_id
  dns_zone_id              = module.network.dns_zone_ids["keyvault"]
  name_suffix              = "ci-kv"
  reader_principal_ids     = local.publisher_enabled ? [module.publisher_identity.principal_id] : []
  purge_protection_enabled = var.key_vault_purge_protection
  tags                     = local.tags
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
  namespaces             = local.create_ns_map
  pe_subnet_id           = module.network.pe_subnet_id
  dns_zone_id            = module.network.dns_zone_ids["eventhub"]
  throughput_units_scale = var.throughput_units_floor
  max_throughput_units   = var.max_throughput_units
  processor_principal_id = module.identity.principal_id
  sender_principal_id    = module.log_sender_identity.principal_id
  sender_enabled         = local.log_sender_enabled
  tags                   = local.tags
}

# Namespaces this instance does NOT create but binds to (create: false) - e.g.
# Azure diagnostics already landing in an Event Hub. Look them up, grant the
# processor Receive, and reach them privately via a PE into this VNet.
data "azurerm_eventhub_namespace" "existing" {
  for_each            = local.existing_ns
  name                = each.value.existing_name
  resource_group_name = each.value.existing_resource_group
}
resource "azurerm_role_assignment" "existing_receiver" {
  for_each             = local.existing_ns
  scope                = data.azurerm_eventhub_namespace.existing[each.key].id
  role_definition_name = "Azure Event Hubs Data Receiver"
  principal_id         = module.identity.principal_id
}
resource "azurerm_private_endpoint" "existing_ehns" {
  for_each            = local.existing_ns
  name                = "${var.name_prefix}-${each.key}-ehns-pe"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = module.network.pe_subnet_id
  private_service_connection {
    name                           = "ehns"
    private_connection_resource_id = data.azurerm_eventhub_namespace.existing[each.key].id
    subresource_names              = ["namespace"]
    is_manual_connection           = false # requires PE-approval rights on the target namespace
  }
  private_dns_zone_group {
    name                 = "ehns"
    private_dns_zone_ids = [module.network.dns_zone_ids["eventhub"]]
  }
  tags = local.tags
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
  # Every namespace (created + bound) as name -> fqdn, and every hub with its
  # namespace + shape. Derived from config/sources.yaml, never literals. The
  # engine registers one trigger per hub; the tftest asserts every hub is
  # consumed and every consumed hub is real.
  namespaces              = { for name, fqdn in local.namespace_fqdns : name => { fqdn = fqdn } }
  hubs                    = local.all_hubs
  redis_host              = module.redis.hostname
  redis_ssl_port          = module.redis.ssl_port
  kv_uri                  = module.keyvault.uri
  app_insights_conn       = module.monitoring.app_insights_conn
  storm_limit             = var.storm_limit
  idempotency_ttl_seconds = var.idempotency_ttl_seconds
  worker_process_count    = var.worker_process_count
  threads_per_worker      = var.threads_per_worker
  signals_sink_url        = var.signals_sink_url
  destinations            = var.destinations
  default_routes          = var.default_routes
  max_event_batch_size    = var.max_event_batch_size
  tags                    = local.tags
}
