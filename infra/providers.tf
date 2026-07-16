# Terraform + provider requirements for the pyre composition.
terraform {
  # 1.7+, not 1.6: infra/tests/plan.tftest.hcl uses `mock_provider`, which is
  # what lets the posture/cost assertions plan the whole graph with no Azure
  # subscription. On 1.6 `terraform test` fails to parse it.
  required_version = ">= 1.7"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
  # The storage account sets shared_access_key_enabled = false (no account keys).
  # Without this flag the provider still reaches the Blob data plane (container
  # creation, the post-create readiness poll) with keys and gets a hard 403
  # "Key based authentication is not permitted". This makes it use the caller's
  # Entra identity instead — which is why the deployer needs a Storage Blob Data
  # role on the account/RG (granted once via az CLI; see tools/LAB.md Part 3).
  storage_use_azuread = true
}
