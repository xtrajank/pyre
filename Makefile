# Optional convenience wrappers. ONE set of commands — pick the instance with ENV
# (default: dev). ENV selects the tfvars (envs/<ENV>.tfvars) and the state key.
# Example:  make apply ENV=prod
# Everything here just runs the same terraform/func/pyre commands the guides show;
# if you don't have `make` (e.g. Windows), run those directly.
ENV       ?= dev
PREFIX    ?= pyre$(ENV)          # must match name_prefix in envs/$(ENV).tfvars
TF        := terraform -chdir=infra

.PHONY: init plan apply destroy output fmt \
        pull build validate test publish deploy-engine deploy-mock

# --- infrastructure (ENV picks the instance) --------------------------------
# Local state by default (matches infra/backend.tf). For a remote azurerm backend,
# uncomment the block in infra/backend.tf and pass its per-instance key, e.g.:
#   make init BACKEND_CONFIG='-backend-config=key=prod.tfstate'
BACKEND_CONFIG ?=
init:    ; $(TF) init $(BACKEND_CONFIG) -reconfigure
plan:    ; $(TF) plan    -var-file=envs/$(ENV).tfvars
apply:   ; $(TF) apply   -var-file=envs/$(ENV).tfvars
destroy: ; $(TF) destroy -var-file=envs/$(ENV).tfvars
output:  ; $(TF) output
fmt:     ; $(TF) fmt -recursive

# --- detections / engine bundle (env-agnostic) ------------------------------
pull:     ; ./cli/pyre pull        # clone the external DaC repo into ./.bundle
build:    ; ./cli/pyre build       # build the LogType->detections index
validate: ; ./cli/pyre validate    # lint the pulled bundle
test:     ; ./cli/pyre test        # run the engine + detection tests

# --- deploy code to an instance (PREFIX must match its name_prefix) ----------
# publish the DaC bundle to Blob (workers hot-reload). Needs `az login` +
# BUNDLE_BLOB_ACCOUNT_URL=https://$(PREFIX)stor.blob.core.windows.net
publish: pull build   ; ./cli/pyre publish
deploy-engine: build  ; cd engine && func azure functionapp publish $(PREFIX)-proc --python
deploy-mock:          ; cd tools/mocks/mock_destination && func azure functionapp publish $(PREFIX)-mockdest --python
