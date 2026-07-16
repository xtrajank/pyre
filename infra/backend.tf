# Remote state: one storage account, one blob per instance (key set at init:
# -backend-config="key=<instance>.tfstate"). Bootstrap the account once, out of
# band - Terraform can't create the account that holds its own state. See the
# guide / infra/global/backend.tf.example. For a throwaway single-instance lab,
# comment this block out to fall back to local state.
terraform {
  backend "azurerm" {
    resource_group_name  = "rg-pyre-dev"
    storage_account_name = "pyretfstate5245" # globally unique; created once, out of band
    container_name       = "tfstate"
    use_azuread_auth     = true # Entra auth to the state store, no account keys
  }
}
