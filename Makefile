# Root entry point.

# Written for GNU Make 3.81, the version macOS ships, so it runs unchanged on a Mac and the VM.
SUB := packages/clinicalomxdb
CP  := infra/control_plane
CMP := infra/compute

# Which deployment: prod (the live hub) or staging. It picks the tfvars file, the state key and
# the saved plan's name: make infra-plan ENV=staging.
ENV ?= prod
# Extra flags for tofu plan, e.g. PLAN_ARGS=-replace=openstack_compute_instance_v2.hub to plan a
# rebuild (docs/operations.md section 6.4).
PLAN_ARGS ?=

# What .envrc must set: the OpenStack application credential, the state bucket's EC2 credential
# and endpoint, and the state passphrase. Both roots need all of these.
ENVRC_VARS := OS_AUTH_TYPE OS_AUTH_URL OS_APPLICATION_CREDENTIAL_ID OS_APPLICATION_CREDENTIAL_SECRET \
              AWS_REGION AWS_ENDPOINT_URL_S3 AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY \
              TF_VAR_state_encryption_passphrase TF_CLI_ARGS_init
# The shared GitHub machine tokens. Only the control plane reads them, and they are not in
# .envrc.example (infra/control_plane/README.md, "The rest of the session"), so they come from
# the shell, or from a line in the tfvars file for the username.
TOKEN_VARS := TF_VAR_ghcr_username TF_VAR_ghcr_packages_read_pat TF_VAR_github_contents_read_pat

comma := ,
TOFU_HINT   := Install OpenTofu with brew install opentofu on a Mac$(comma) or follow https://opentofu.org/docs/intro/install/ on Linux$(comma) then run this again.
DIRENV_HINT := Install it with brew install direnv on a Mac$(comma) or sudo apt install direnv on Ubuntu$(comma) then run this again.
UV_HINT     := Install it with brew install uv on a Mac$(comma) or follow https://docs.astral.sh/uv/getting-started/installation/$(comma) then run this again.

# Print the label $(1) and the command $(2), then run the command. On failure print $(3), which
# says what happened and what to do, and stop. $(2) and $(3) must not contain a single quote.
define run
@printf '\n== %s\n$$ %s\n' '$(1)' '$(2)'
@$(2) || { echo 'STOP: $(3)'; exit 1; }
endef

# Stop unless the command $(1) is on PATH; $(2) says how to install it.
define need
@command -v $(1) >/dev/null 2>&1 || { echo 'STOP: $(1) is not installed (it is not on PATH). $(2)'; exit 1; }
endef

# Stop unless the file $(1) exists; $(2) says where to get it.
define need_file
@test -f $(1) || { echo 'STOP: $(1) does not exist. $(2)'; exit 1; }
endef

# Stop, naming each variable in $(1) that is unset or still holds the example's placeholder, and
# print $(2). With $(3), a tfvars file, a variable that file sets itself (ghcr_username = ...) is
# not needed in the environment. Runs inside direnv exec, so it sees what tofu will see.
define need_vars
@direnv exec $(CP) sh -c 'bad=; for n in $(1); do \
  [ -n "$(3)" ] && grep -Eq "^[[:space:]]*$${n#TF_VAR_}[[:space:]]*=" "$(3)" && continue; \
  case "$$(printenv $$n)" in "" | replace-me | *example.edu*) bad="$$bad $$n";; esac; done; \
  [ -z "$$bad" ] || { echo "STOP: these are not set, or still hold the placeholder from the example:$$bad"; \
  echo "$(2)"; exit 1; }' 2>/dev/null
endef

# Stop unless direnv can load $(CP)/.envrc cleanly and it sets every variable in ENVRC_VARS.
# direnv's output is captured rather than shown: a malformed .envrc line makes the shell echo that
# line back, and the line can hold a secret.
define need_env
$(call need,direnv,$(DIRENV_HINT))
$(call need_file,$(CP)/.envrc,Run make infra-env to create it.)
@out=$$(DIRENV_LOG_FORMAT='direnv: %s' direnv exec $(CP) true 2>&1); rc=$$?; \
case "$$out" in *' is blocked'*) \
  echo 'STOP: direnv has not approved $(CP)/.envrc$(comma) or the file changed since. Run make infra-env.'; exit 1;; esac; \
if [ $$rc -ne 0 ] || printf '%s\n' "$$out" | grep -qv '^direnv: '; then \
  echo 'STOP: $(CP)/.envrc has a malformed line. Check that every line reads export NAME=value with no space around the = sign$(comma) then run make infra-env.'; exit 1; fi
$(call need_vars,$(ENVRC_VARS),Set each one in $(CP)/.envrc (the comment above each line says where the value comes from)$(comma) then run make infra-env.)
endef

# Stop unless ENV names a deployment, so a typo cannot open a new, empty state.
define need_known_env
@case '$(ENV)' in prod|staging) ;; *) echo 'STOP: ENV=$(ENV) is not a deployment. Use ENV=prod (the default) or ENV=staging.'; exit 1;; esac
endef

# The plan sequence for one root, after its own checks: init, static checks, plan to a file.
# $(1) root directory, $(2) the control plane directory relative to it (direnv loads its .envrc),
# $(3) state key, $(4) var file, $(5) target prefix for messages.
define plan_root
$(call run,Connect to the saved state for $(ENV) (the first run also downloads the providers),cd $(1) && direnv exec $(2) tofu init -input=false -reconfigure -backend-config="key=$(3)",tofu init failed with the error above. Nothing was changed. The usual causes are no network$(comma) or a wrong value in $(CP)/.envrc: the AWS_ pair$(comma) AWS_ENDPOINT_URL_S3 or the state passphrase.)
$(call run,Check the .tf files are formatted,cd $(1) && direnv exec $(2) tofu fmt -check,a .tf file listed above is not formatted. Nothing was changed. Run cd $(1) && tofu fmt$(comma) check the result with git diff$(comma) then run this again.)
$(call run,Check the configuration is valid,cd $(1) && direnv exec $(2) tofu validate,the configuration has the error shown above. Nothing was changed. If you edited no .tf file$(comma) pull the latest main and run this again.)
$(call run,Work out what would change and save it to $(1)/$(ENV).plan. This changes nothing live,cd $(1) && direnv exec $(2) tofu plan -input=false -var-file=$(4) -out=$(ENV).plan$(if $(PLAN_ARGS), $(PLAN_ARGS)),tofu plan failed with the error above. A plan changes nothing. An error about decrypting the state means TF_VAR_state_encryption_passphrase is wrong; one about authentication means an OS_ value in $(CP)/.envrc is.)
@printf '\nRead the plan above. "Plan: N to add, N to change, N to destroy" is the summary.\n'
@printf 'Nothing has changed yet. To make these changes, run: make $(5)-apply ENV=$(ENV)\n'
endef

# Show the saved plan, take a typed confirmation, apply it, and remove it. Not repeatable: a saved
# plan is used up once applied, so the second run refuses and asks for a fresh plan.
# $(1) root directory, $(2) the control plane directory relative to it, $(3) target prefix.
define apply_root
$(call need,tofu,$(TOFU_HINT))
$(need_known_env)
$(need_env)
$(call need_file,$(1)/$(ENV).plan,There is no saved plan to apply: run make $(3)-plan ENV=$(ENV) first and read what it prints.)
@test -t 0 || { echo 'STOP: this asks you to type a confirmation$(comma) so run it yourself in a terminal$(comma) not from a script.'; exit 1; }
$(call run,Show the saved plan once more,cd $(1) && direnv exec $(2) tofu show $(ENV).plan,tofu could not read the saved plan. Nothing was changed. Run make $(3)-plan ENV=$(ENV) again.)
@printf '\nThis changes LIVE infrastructure in $(ENV). Type $(ENV) and press Enter to apply the plan above,\nor press Enter alone to stop: '; \
read -r answer; [ "$$answer" = '$(ENV)' ] || { echo 'Stopped. Nothing was applied.'; exit 1; }
$(call run,Apply the saved plan,cd $(1) && direnv exec $(2) tofu apply -input=false $(ENV).plan,tofu apply stopped with the error above. Part of the plan may already be applied. Run make $(3)-plan ENV=$(ENV) to see what is left.)
$(call run,Remove the used plan so it cannot be applied twice,rm $(1)/$(ENV).plan,could not remove the used plan. Delete $(1)/$(ENV).plan by hand.)
endef

.DEFAULT_GOAL := help

.PHONY: help site-check infra-env infra-plan infra-apply compute-plan compute-apply

help:
	@echo "Hub services: packages/clinicalomxdb/Makefile. Each of these also runs from here."
	@echo ""
	@$(MAKE) --no-print-directory -C $(SUB) help
	@echo ""
	@echo "Workstation: site check and infrastructure (this Makefile). Run these on your own"
	@echo "machine, at the repository root, in this order the first time."
	@echo "  make site-check     Validate site.yaml. Writes nothing; safe to repeat."
	@echo "  make infra-env      Create and approve infra/control_plane/.envrc, tofu's credentials. Safe to repeat."
	@echo "  make infra-plan     Preview hub VM changes and save them as a plan. Changes nothing live; safe to repeat."
	@echo "  make infra-apply    Apply the saved hub plan after you type the ENV name. CHANGES LIVE INFRASTRUCTURE."
	@echo "  make compute-plan   Preview dev box changes and save them as a plan. Changes nothing live; safe to repeat."
	@echo "  make compute-apply  Apply the saved dev box plan after you type the ENV name. CHANGES LIVE INFRASTRUCTURE."
	@echo "  An apply uses up its plan, so run the plan target again before the next apply."
	@echo "  Options: ENV=staging (default prod) picks the deployment; PLAN_ARGS='...' adds flags to tofu plan."
	@echo "  Each target prints every command before it runs it, and stops with what to do when something"
	@echo "  it needs is missing."

site-check:
	$(call need,uv,$(UV_HINT))
	$(call need_file,site.yaml,Copy the filled-in site.yaml from the lab vault to the repository root$(comma) or copy site.example.yaml to site.yaml and fill in every key.)
	$(call run,Validate site.yaml$(comma) which tofu reads for the hub address and the HPC address,uv run scripts/site/render.py --check site.yaml,site.yaml has the problems named above. Fix each key it names$(comma) then run this again.)

infra-env:
	$(call need,direnv,$(DIRENV_HINT))
	@if [ -f $(CP)/.envrc ]; then printf '\n$(CP)/.envrc exists; leaving it as it is.\n'; else \
	  printf '\n== %s\n$$ %s\n' 'Create .envrc from its example$(comma) readable only by you' \
	    'cp $(CP)/.envrc.example $(CP)/.envrc && chmod 600 $(CP)/.envrc'; \
	  cp $(CP)/.envrc.example $(CP)/.envrc && chmod 600 $(CP)/.envrc || exit 1; fi
	@left=$$(grep -E '^export [A-Za-z0-9_]+=.*(replace-me|example\.edu)' $(CP)/.envrc | sed 's/^export //; s/=.*//'); \
	if [ -n "$$left" ]; then \
	  echo ''; \
	  echo 'STOP: these lines in $(CP)/.envrc still hold the placeholder from the example:'; \
	  printf '  %s\n' $$left; \
	  echo 'Open $(CP)/.envrc in a text editor and replace each placeholder with the real value.'; \
	  echo 'The comment above each line says where its value comes from. The lab vault holds the state'; \
	  echo 'passphrase (clinicalomx/infra/state-passphrase) and the state bucket key pair'; \
	  echo '(clinicalomx/infra/tfstate-backend); the OS_APPLICATION_CREDENTIAL pair is your own.'; \
	  echo 'Save the file, then run make infra-env again.'; \
	  exit 1; fi
	$(call run,Approve this exact version of .envrc$(comma) so direnv will load it,direnv allow $(CP),direnv could not approve the file. Run direnv allow $(CP) yourself to see why.)
	$(need_env)
	@printf '\nReady: direnv loads $(CP)/.envrc into every tofu command these targets run.\n'
	@printf 'Next: make infra-plan (the hub VM) or make compute-plan (the dev boxes).\n'
	@printf 'Each edit to .envrc withdraws the approval, so run make infra-env again after one.\n'

infra-plan: site-check
	$(call need,tofu,$(TOFU_HINT))
	$(need_known_env)
	$(need_env)
	$(call need_file,$(CP)/$(ENV).tfvars,Copy prod.tfvars from the lab vault entry clinicalomx/infra/prod.tfvars$(comma) or for staging copy $(CP)/staging.tfvars.example to $(CP)/staging.tfvars and fill it in.)
	$(call need_vars,$(TOKEN_VARS),These are the shared GitHub machine tokens (infra/secrets/README.md$(comma) GitHub table). For each one run IFS= read -rs NAME && export NAME in this shell$(comma) paste the value (it does not show)$(comma) press Enter$(comma) then run this again.,$(CP)/$(ENV).tfvars)
	@# tofu copies the three hub env files into Barbican, so it needs the real ones. A tfvars file
	@# that moves them (*_env_path) is left to tofu to check.
	@grep -q '_env_path' $(CP)/$(ENV).tfvars || for f in hub-database hub-pipelines hub-ops; do \
	  test -f $(SUB)/$$f/.env || { echo "STOP: $(SUB)/$$f/.env does not exist. tofu stores it in Barbican$(comma) so it must be the real file: copy it from the lab vault entry clinicalomx/env/$$f.env."; exit 1; }; done
	$(call plan_root,$(CP),.,$(ENV)/terraform.tfstate,$(ENV).tfvars,infra)

infra-apply:
	$(call apply_root,$(CP),.,infra)
	@printf '\nApplied. Check the hub with the steps under "Verify after apply" in $(CP)/README.md.\n'

compute-plan: site-check
	$(call need,tofu,$(TOFU_HINT))
	$(need_known_env)
	$(need_env)
	$(call need_file,$(CMP)/compute.tfvars,Copy $(CMP)/compute.tfvars.example to $(CMP)/compute.tfvars and fill it in ($(CMP)/README.md$(comma) Launch a box).)
	$(call plan_root,$(CMP),../control_plane,$(ENV)/compute/terraform.tfstate,compute.tfvars,compute)

compute-apply:
	$(call apply_root,$(CMP),../control_plane,compute)
	$(call run,Print the address of each box and the ssh line to send each user,cd $(CMP) && direnv exec ../control_plane tofu output dev_boxes,the apply finished but tofu could not print the outputs. Run make compute-plan to check the state.)

# Forward anything else. Command-line variable overrides reach the sub-make through MAKEFLAGS, so
# `make tunnel-model MODEL_PORT=8001` passes through.
%:
	@printf '$$ make -C %s %s\n' '$(SUB)' '$@'
	@$(MAKE) --no-print-directory -C $(SUB) $@

# Do not try to remake this file through the catch-all rule.
Makefile: ;
