variable "name_prefix" { type = string }
variable "location" { type = string }
variable "resource_group_name" { type = string }

variable "name" {
  type        = string
  description = "Suffix identifying what this identity is for (e.g. \"sender\", \"publisher\"). Combined with name_prefix for the resource name when mode = \"federated\"."
}

variable "mode" {
  type        = string
  default     = "managed_identity"
  description = <<-EOT
    How this external actor authenticates to Azure:
      "managed_identity" - the actor already runs AS an Azure resource (a VM,
        VMSS, Container App, AKS pod, another Function App, a self-hosted CI
        agent on Azure compute, ...) and already has an identity attached.
        This module creates nothing; it passes `principal_id` straight
        through so the caller can grant it roles.
      "federated"         - the actor runs OUTSIDE Azure (a laptop, GitHub
        Actions, Azure DevOps Microsoft-hosted agents, any OIDC-capable CI)
        and has no Azure identity of its own to attach. This module
        provisions a user-assigned managed identity plus a federated
        credential trusting the actor's OIDC issuer/subject, so it can
        exchange its own OIDC token for an Azure AD token - no client secret
        stored anywhere.
  EOT
  validation {
    condition     = contains(["managed_identity", "federated"], var.mode)
    error_message = "mode must be \"managed_identity\" or \"federated\"."
  }
}

variable "principal_id" {
  type        = string
  default     = ""
  description = "Required when mode = \"managed_identity\": the object ID of the actor's own existing identity. Ignored when mode = \"federated\"."
}

variable "federated_credentials" {
  type = list(object({
    name     = string
    issuer   = string
    subject  = string
    audience = optional(list(string), ["api://AzureADTokenExchange"])
  }))
  default     = []
  description = <<-EOT
    Required when mode = "federated": one entry per OIDC trust to establish
    (e.g. one for a GitHub Actions branch, another for an ADO service
    connection). Ignored when mode = "managed_identity".
      - GitHub Actions: issuer = "https://token.actions.githubusercontent.com",
        subject = "repo:<org>/<repo>:ref:refs/heads/<branch>" (or
        "repo:<org>/<repo>:environment:<name>" for an environment-scoped run).
      - Azure DevOps (manual "workload identity federation" service
        connection backed by this identity): create the service connection
        first, choosing "existing app registration or user-assigned managed
        identity" and this identity's client/tenant ID (see the client_id/
        tenant_id outputs) - ADO then displays the issuer URL and subject
        identifier to trust; put those here and re-apply.
  EOT
}

variable "tags" {
  type    = map(string)
  default = {}
}
