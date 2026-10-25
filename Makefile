# Optional deploy helper for running this on a home server over SSH.
# Not needed to use the app — see the README quickstarts.
#
# Set your own target, either inline or in the environment:
#   make deploy DEPLOY_HOST=you@server DEPLOY_DIR=~/talk-radio
#
# Ships the committed tree (git archive HEAD), so commit before deploying —
# a dirty working tree only earns a warning.

DEPLOY_HOST ?=
DEPLOY_DIR  ?= ~/talk-radio
DEPLOY_PORT ?= 8080
DOPPLER_PROJECT ?= talk-radio
DOPPLER_CONFIG  ?= dev

.PHONY: deploy logs status test check-host sync-secrets

check-host:
	@if [ -z "$(DEPLOY_HOST)" ]; then \
		echo "DEPLOY_HOST is unset. Usage: make $(MAKECMDGOALS) DEPLOY_HOST=user@server [DEPLOY_DIR=~/talk-radio]"; \
		exit 1; \
	fi

test:
	python -m pytest

deploy: check-host
	@if ! git diff-index --quiet HEAD --; then \
		echo "warning: uncommitted changes will NOT be deployed (git archive ships HEAD)"; \
	fi
	git archive HEAD | ssh $(DEPLOY_HOST) 'tar -x -C $(DEPLOY_DIR)'
	ssh $(DEPLOY_HOST) 'cd $(DEPLOY_DIR) && docker compose up -d --build'
	@sleep 4
	@curl -sf --connect-timeout 5 http://$$(echo $(DEPLOY_HOST) | cut -d@ -f2):$(DEPLOY_PORT)/api/status > /dev/null \
		&& echo "deployed: http://$$(echo $(DEPLOY_HOST) | cut -d@ -f2):$(DEPLOY_PORT)" \
		|| echo "deployed, but status check failed — run 'make logs'"

# Copy the gpodder credentials from Doppler into the server's .env (mode
# 600), replacing any earlier copy. The server refuses anything but both
# keys, so a Doppler or parse failure leaves .env untouched. Values travel over ssh stdin, never argv
# or the terminal. Single-quoted so compose takes them literally — a value
# containing a single quote is refused rather than mangled. Run it again
# after rotating, then `make deploy` to recreate the container.
sync-secrets: check-host
	@doppler secrets download --no-file --format json \
		--project $(DOPPLER_PROJECT) --config $(DOPPLER_CONFIG) \
	| python3 -c 'import json,sys; s=json.load(sys.stdin); keys=("GPODDER_USER","GPODDER_PASSWORD"); missing=[k for k in keys if not s.get(k)]; bad=[k for k in keys if "\x27" in s.get(k,"")]; missing and sys.exit("missing in Doppler: "+", ".join(missing)); bad and sys.exit("single quote not supported in: "+", ".join(bad)); print("".join(f"{k}=\x27{s[k]}\x27\n" for k in keys), end="")' \
	| ssh $(DEPLOY_HOST) 'set -e; umask 077; f=$(DEPLOY_DIR)/.env; new=$$(cat); \
		[ "$$(printf "%s\n" "$$new" | grep -c "^GPODDER_")" = 2 ] || { echo "nothing valid received; .env unchanged" >&2; exit 1; }; \
		touch "$$f"; { grep -v "^GPODDER_" "$$f" || true; printf "%s\n" "$$new"; } > "$$f.tmp"; mv "$$f.tmp" "$$f"; chmod 600 "$$f"'
	@echo "synced GPODDER_USER, GPODDER_PASSWORD to $(DEPLOY_HOST):$(DEPLOY_DIR)/.env"

logs: check-host
	ssh $(DEPLOY_HOST) 'cd $(DEPLOY_DIR) && docker compose logs --tail 50 -f'

status: check-host
	@curl -s http://$$(echo $(DEPLOY_HOST) | cut -d@ -f2):$(DEPLOY_PORT)/api/status | python3 -m json.tool
