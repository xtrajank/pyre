# module: keyvault

A Key Vault: RBAC (not access policies), private endpoint, purge protection. Instantiated twice in infra/main.tf - engine-runtime secrets and CI-only secrets - with disjoint readers.
