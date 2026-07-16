# Offline plan assertions for the pyre composition.
#
# A MOCK azurerm provider, so this runs a real `terraform plan` over the whole
# graph with no subscription, no credentials, and no cost. It pins the two
# things a plan review is supposed to catch by eye and reliably doesn't:
#
#   security - nothing is reachable from the public internet, nothing accepts a
#              shared key/SAS/password, and the engine vault and the CI vault
#              never share a reader.
#   cost     - the SKUs each cost_profile actually selects.
#
# ...plus the one thing a plan CANNOT catch, because both sides are valid
# strings: that the hub the processor subscribes to is a hub sources.yaml
# actually creates.
#
# Run: terraform -chdir=infra test

mock_provider "azurerm" {
  # The real data source needs a live subscription; the default mock returns a
  # random 8-char string, which azurerm_key_vault rejects as a non-UUID tenant.
  mock_data "azurerm_client_config" {
    defaults = {
      tenant_id       = "00000000-0000-0000-0000-000000000001"
      subscription_id = "00000000-0000-0000-0000-000000000002"
      object_id       = "00000000-0000-0000-0000-000000000003"
      client_id       = "00000000-0000-0000-0000-000000000004"
    }
  }
}

variables {
  location            = "eastus2"
  resource_group_name = "rg-pyre-test"
  name_prefix         = "pyretest"
}

# --- greenfield plannability --------------------------------------------------
#
# Every computed attribute is unknown under a mock provider, which is exactly
# the state of a first apply into an empty subscription. A count/for_each that
# depends on one of those (e.g. a Managed Identity principal_id created in the
# same apply) fails the plan outright, so this run is the regression test for
# "terraform apply works on a clean subscription at all".

run "greenfield_plan_succeeds" {
  command = plan
}

run "federated_identities_plan_succeeds" {
  command = plan
  variables {
    publisher = {
      mode = "federated"
      federated_credentials = [{
        name    = "ado-pyre-publisher"
        issuer  = "https://vstoken.dev.azure.com/xxx"
        subject = "sc://org/project/conn"
      }]
    }
    log_sender = {
      mode         = "managed_identity"
      principal_id = "00000000-0000-0000-0000-00000000beef"
    }
  }
}

# --- cost: the test profile must be the cheap one -----------------------------

run "test_profile_picks_cheapest_skus" {
  command = plan
  variables {
    cost_profile = "test"
  }

  assert {
    condition     = module.redis.sku_name == "Balanced_B0"
    error_message = "test profile must select Azure Managed Redis Balanced_B0, the smallest/cheapest SKU"
  }

  assert {
    condition     = module.eventhub.capacity == 1 && module.eventhub.auto_inflate_enabled == false
    error_message = "test profile must pin Event Hubs at 1 throughput unit with auto-inflate off, or a quiet lab can bill for 20"
  }

  assert {
    condition     = module.function_app.maximum_instance_count == 40
    error_message = "test profile must cap Flex at 40 instances"
  }

  assert {
    condition     = module.function_app.sku_name == "FC1"
    error_message = "the processor must run on Flex Consumption (FC1), which scales to zero when a source goes quiet"
  }

  assert {
    condition     = module.storage.account_replication_type == "LRS"
    error_message = "checkpoints/bundles are reconstructible; paying for GRS replication buys nothing"
  }
}

# --- cost: the scale profile must actually scale ------------------------------

run "scale_profile_scales_up" {
  command = plan
  variables {
    cost_profile = "scale"
  }

  assert {
    condition     = module.redis.sku_name == "Balanced_B5"
    error_message = "scale profile must select a production-sized Azure Managed Redis SKU (Balanced_B5 floor), not the lab's Balanced_B0"
  }

  assert {
    condition     = module.eventhub.auto_inflate_enabled == true
    error_message = "scale profile must enable Event Hubs auto-inflate or ingest throttles at the floor"
  }

  assert {
    condition     = module.function_app.maximum_instance_count == 1000
    error_message = "scale profile must let Flex reach 1000 instances"
  }
}

# --- security: nothing is publicly reachable ----------------------------------

run "nothing_is_publicly_reachable" {
  command = plan

  assert {
    condition     = module.redis.public_network_access_enabled == false
    error_message = "Redis must not be reachable from the public internet"
  }

  assert {
    condition     = module.eventhub.public_network_access_enabled == false
    error_message = "the Event Hubs namespace must not be reachable from the public internet"
  }

  assert {
    condition     = module.storage.public_network_access_enabled == false
    error_message = "the storage account must not be reachable from the public internet"
  }

  assert {
    condition     = module.keyvault.public_network_access_enabled == false && module.ci_keyvault.public_network_access_enabled == false
    error_message = "neither Key Vault may be reachable from the public internet"
  }

  assert {
    condition     = module.function_app.public_network_access_enabled == false
    error_message = "the Function App must have no public inbound; the Event Hub trigger pulls"
  }

  assert {
    condition     = alltrue([for t in module.storage.container_access_types : t == "private"])
    error_message = "every blob container must be private; a public one would expose checkpoints or detection logic"
  }
}

# --- there is no public network path, ever -----------------------------------
#
# No deployer IP allowlist exists: every resource is private and reached only
# from inside the VNet. This pins that nothing can regress to a public endpoint -
# the regression test for someone reintroducing an internet-reachable hole.

run "no_resource_is_ever_publicly_reachable" {
  command = plan

  assert {
    condition = alltrue([
      module.eventhub.public_network_access_enabled == false,
      module.storage.public_network_access_enabled == false,
      module.function_app.public_network_access_enabled == false,
      module.redis.public_network_access_enabled == false,
      module.keyvault.public_network_access_enabled == false,
      module.ci_keyvault.public_network_access_enabled == false,
    ])
    error_message = "every resource must be private; deploy from inside the VNet, never a public endpoint"
  }
}

# --- security: identity only, never a shared key ------------------------------

run "no_shared_key_auth_anywhere" {
  command = plan

  assert {
    condition     = module.eventhub.local_authentication_enabled == false
    error_message = "Event Hubs must force Entra auth; SAS keys are a bearer secret that cannot be rotated centrally"
  }

  assert {
    condition     = module.redis.entra_auth_enabled == true && module.redis.non_ssl_port_enabled == false && module.redis.minimum_tls_version == "1.2"
    error_message = "Redis must use Entra auth over TLS 1.2+ with the non-TLS port closed"
  }

  assert {
    condition     = module.storage.shared_access_key_enabled == false
    error_message = "storage must disable shared-key auth so only Managed Identity works"
  }

  assert {
    condition     = module.storage.https_traffic_only_enabled == true && module.storage.min_tls_version == "TLS1_2"
    error_message = "storage must require HTTPS and TLS 1.2+"
  }

  assert {
    condition     = module.keyvault.rbac_authorization_enabled == true && module.ci_keyvault.rbac_authorization_enabled == true
    error_message = "Key Vault must use RBAC, not legacy access policies"
  }

  assert {
    condition     = module.keyvault.purge_protection_enabled == true && module.ci_keyvault.purge_protection_enabled == true
    error_message = "Key Vault must have purge protection so a deleted secret is recoverable"
  }
}

# --- security: no secret is inlined into app settings -------------------------

run "no_secret_literals_in_app_settings" {
  command = plan

  # A destination WITH a secret, so this asserts against a real generated token
  # setting rather than passing vacuously on an empty destinations map.
  variables {
    destinations = {
      mock      = { url = "https://mock.example/api/alert" }
      torq_prod = { url = "https://torq.example/hook", token_secret = "torq-prod-token" }
    }
  }

  # Tokens must arrive as Key Vault references, resolved by the platform at
  # runtime. A literal would be readable by anyone with Reader on the app.
  assert {
    condition = alltrue([
      for k, v in module.function_app.app_settings :
      startswith(v, "@Microsoft.KeyVault(") if endswith(k, "_TOKEN")
    ])
    error_message = "every *_TOKEN app setting must be a Key Vault reference, never a literal secret"
  }

  # Only the KEY is asserted here, not the value. The value embeds the vault URI,
  # which is computed and therefore unknown at plan - and Terraform can only
  # reason about the known PREFIX of such a string, which is exactly what the
  # startswith assertion above uses. So: this proves the setting is generated
  # and named from the destination; the assertion above proves it is a Key Vault
  # reference rather than a literal. Between them the property holds.
  assert {
    condition     = contains(keys(module.function_app.app_settings), "DESTINATION_TORQ_PROD_TOKEN")
    error_message = "a destination declaring token_secret must get a DESTINATION_<NAME>_TOKEN setting"
  }
}

# --- destinations are generic, not vendor-shaped ------------------------------
#
# The infrastructure must not know what Torq is. Destination settings are
# derived from the `name` in config/destinations.yaml, so a new destination -
# of any kind - is a tfvars entry, not a Terraform change.

run "destination_settings_are_derived_from_the_name" {
  command = plan
  variables {
    destinations = {
      mock             = { url = "https://mock.example/api/alert" }
      "acme-soar-prod" = { url = "https://acme.example/hook", token_secret = "acme-token" }
    }
    default_routes = ["acme-soar-prod"]
  }

  assert {
    condition     = module.function_app.app_settings["DESTINATION_MOCK_URL"] == "https://mock.example/api/alert"
    error_message = "a destination's URL must be published as DESTINATION_<NAME>_URL"
  }

  # Dashes are illegal in an env var name, so the key must be normalised.
  assert {
    condition     = module.function_app.app_settings["DESTINATION_ACME_SOAR_PROD_URL"] == "https://acme.example/hook"
    error_message = "a destination name's dashes must become underscores in its app-setting key"
  }

  assert {
    condition     = module.function_app.app_settings["DEFAULT_ROUTES"] == "acme-soar-prod"
    error_message = "default_routes must be published for the engine; routing must not be inferred from `env`"
  }
}

run "a_destination_without_a_secret_gets_no_token_setting" {
  command = plan
  variables {
    destinations = {
      mock = { url = "https://mock.example/api/alert" }
    }
  }

  # A Key Vault reference to a secret nobody created resolves to a broken
  # literal and fails at dispatch time. No secret declared -> no setting -> the
  # adapter sees an unset env var and fails closed, at cold start, loudly.
  assert {
    condition     = !contains(keys(module.function_app.app_settings), "DESTINATION_MOCK_TOKEN")
    error_message = "a destination with no token_secret must not get a token app setting at all"
  }
}

# --- a lab instance must be structurally unable to page production ------------

run "a_dev_instance_declares_no_production_destination" {
  command = plan
  variables {
    env = "dev"
    destinations = {
      mock = { url = "https://mock.example/api/alert" }
    }
    default_routes = ["mock"]
  }

  # The real guarantee: not the `env` string, and not destinations.yaml's
  # `enabled` flag - simply that this instance has no production URL or token to
  # send with, and does not route there by default.
  assert {
    condition = length([
      for k, v in module.function_app.app_settings : k
      if startswith(k, "DESTINATION_") && !startswith(k, "DESTINATION_MOCK_")
    ]) == 0
    error_message = "a dev instance must publish no destination settings other than its own sink"
  }

  assert {
    condition     = module.function_app.app_settings["DEFAULT_ROUTES"] == "mock"
    error_message = "a dev instance must default-route only to its mock sink"
  }
}

# --- security: the two vaults must not share a reader -------------------------
#
# The engine vault holds Torq tokens; the CI vault holds the DaC PAT. They are
# two vaults specifically so that compromising one identity cannot read the
# other's secrets, which only holds if their reader sets stay disjoint.
#
# NOT asserted here, deliberately. The readers are Managed Identity object IDs,
# unknown until apply, so a plan-time condition on them is unevaluable - and
# `command = apply` doesn't help either: the mock provider fills computed ids
# with random 8-char strings that azurerm's own client-side ID validators
# reject. The property is enforced by construction in infra/main.tf instead
# (module.keyvault receives only module.identity; module.ci_keyvault receives
# only module.publisher_identity) and is checked by review there.

# --- correctness: the processor consumes EXACTLY the configured hubs ----------
#
# The failure this catches is invisible to a plan: a trigger's hub name and the
# hub names are both just strings. A hub the processor consumes but nothing
# creates binds to nothing; a created hub with no consumer ingests at full rate
# and is evaluated by nobody. Both are silent. HUBS_CONFIG and the created hubs
# both derive from config/sources.yaml, and this pins them equal.

run "processor_consumes_exactly_the_configured_hubs" {
  command = plan

  assert {
    condition     = length(setsubtract(module.function_app.eventhub_names, [for h in local.all_hubs : h.hub])) == 0
    error_message = "the processor consumes hub(s) config/sources.yaml does not declare"
  }

  assert {
    condition     = length(setsubtract([for h in local.all_hubs : h.hub], module.function_app.eventhub_names)) == 0
    error_message = "config/sources.yaml declares hub(s) the processor does not consume; they would ingest logs no detection ever sees"
  }
}

# --- correctness: created namespaces actually create their hubs ---------------

run "created_namespaces_create_their_hubs" {
  command = plan

  assert {
    condition = length(setsubtract(
      flatten([for name, ns in local.create_ns_map : keys(ns.hubs)]),
      module.eventhub.hub_names
    )) == 0
    error_message = "a created namespace declares a hub the eventhub module did not create"
  }
}

# --- correctness: exactly one catch-all hub PER namespace --------------------
#
# Each feed's default hub is what makes a log type with no dedicated hub
# legitimate rather than a silent gap. With none, an undeclared log type has
# nowhere to land; with two, "the default" is ambiguous.

run "each_namespace_has_exactly_one_default_hub" {
  command = plan

  assert {
    condition     = alltrue([for ns, defs in local.ns_default_hubs : length(defs) == 1])
    error_message = "each namespace in config/sources.yaml must declare EXACTLY ONE hub with `default: true` (its catch-all)"
  }
}

# --- cost: each namespace's catch-all is its cheapest hub --------------------
#
# The catch-all carries the long tail, so it should not be sized like a
# firehose. If a high-volume source falls through to it, its partition count
# caps that source's throughput - so keep it the floor, deliberately.

run "each_default_hub_is_its_namespaces_cheapest" {
  command = plan

  assert {
    condition = alltrue(flatten([
      for ns in local.namespaces : [
        for h in ns.hubs :
        h.partitions >= [for d in ns.hubs : d.partitions if try(d.default, false)][0]
      ]
    ]))
    error_message = "each namespace's default/catch-all hub must have the fewest partitions of any hub in that namespace"
  }
}
