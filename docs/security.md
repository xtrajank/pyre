# Security posture

Non-negotiables baked into the IaC. If a change would violate one of these, the plan should fail review.

1. **No public-facing anything.** Every PaaS resource (Event Hubs, Redis, Key Vault, Storage, Function App) sets `public_network_access_enabled = false` and is reachable only via a **Private Endpoint** inside the VNet. The Function App has no public HTTP surface — its trigger pulls from Event Hubs. The only egress is to your alert destination (Torq), via a controlled outbound path.
2. **Managed Identity for all service-to-service auth.** No connection strings, no account keys, no SAS tokens in app settings, code, or Terraform variables. RBAC role assignments are least-privilege and declared in IaC (e.g. the processor gets only `Event Hubs Data Receiver` on the specific hub, `Key Vault Secrets User`, `Redis` data access).
3. **Secrets only in Key Vault.** Torq tokens and any Cribl credentials live in Key Vault (private endpoint) and are consumed via Key Vault references or fetched at runtime by the MI. `*.tfvars` with real secrets are git-ignored; only `*.tfvars.example` are committed.

   **Known exception — `signals_sink_url`.** The signals write-back POSTs the
   **full raw event body** to this URL with **no `Authorization` header**
   (`engine/pyre_engine/signals.py`), so the URL itself is the only thing
   guarding the endpoint — i.e. a bearer secret. It is a plain Terraform
   variable, so it lands in **tfstate and app settings in cleartext**, which
   violates the spirit of this rule. If your Cribl HTTP source authenticates via
   a token-in-URL, treat that value as a secret (Key Vault reference) and keep
   the endpoint off the public internet. Anyone who learns the URL can also
   **inject forged signals** into the lake.
4. **State is protected.** Terraform state lives in a private, locked storage account. Treat it as sensitive.
5. **Everything is tagged** (`system`, `env`, `module`, `owner`) so cost and blast radius are attributable, and diagnostic settings ship every resource's logs to Log Analytics.
6. **Least-privilege CI.** The deploy identity is scoped to the target resource group(s) only, and prod applies are gated behind environment protection/approvals.
7. **Egress control.** Outbound to Torq goes through a known NAT/egress with an allowlist; the system does not make arbitrary outbound calls.
