# module: function_app

Single-concern Terraform module. Private by default (public network access disabled), Managed-Identity/RBAC auth, no static keys. Consumed by env compositions in ../../envs/. Replaceable in isolation; outputs are the only coupling.
