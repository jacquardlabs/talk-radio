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

.PHONY: deploy logs status test check-host

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

logs: check-host
	ssh $(DEPLOY_HOST) 'cd $(DEPLOY_DIR) && docker compose logs --tail 50 -f'

status: check-host
	@curl -s http://$$(echo $(DEPLOY_HOST) | cut -d@ -f2):$(DEPLOY_PORT)/api/status | python3 -m json.tool
