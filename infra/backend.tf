# Remote Terraform state, ONE state file per instance. The state store is shared;
# each instance gets its own state blob, chosen by `key` at init time. Bootstrap
# the state storage once (see infra/global/backend.tf.example), then:
#
#   terraform -chdir=infra init -backend-config="key=<instance>.tfstate"
#
# e.g. key=dev.tfstate for the dev instance, key=prod.tfstate for prod.
terraform {
  backend "azurerm" {
    resource_group_name  = "rg-tfstate"
    storage_account_name = "pyretfstate" # <- state storage account
    container_name       = "tfstate"
    use_azuread_auth     = true # auth to the state store with your Entra login, no keys
    # key is supplied per instance at `terraform init -backend-config="key=..."`
  }
}
