# Deployment to the home server. Ships the committed tree (git archive HEAD),
# so commit before deploying — a dirty working tree only earns a warning.

DEPLOY_HOST ?= tehdot@mothership
DEPLOY_DIR  ?= ~/talk-radio
DEPLOY_PORT ?= 8005

.PHONY: deploy logs status

deploy:
	@if ! git diff-index --quiet HEAD --; then \
		echo "warning: uncommitted changes will NOT be deployed (git archive ships HEAD)"; \
	fi
	git archive HEAD | ssh $(DEPLOY_HOST) 'tar -x -C $(DEPLOY_DIR)'
	ssh $(DEPLOY_HOST) 'cd $(DEPLOY_DIR) && docker compose up -d --build'
	@sleep 4
	@curl -sf --connect-timeout 5 http://$$(echo $(DEPLOY_HOST) | cut -d@ -f2):$(DEPLOY_PORT)/api/status > /dev/null \
		&& echo "deployed: http://$$(echo $(DEPLOY_HOST) | cut -d@ -f2):$(DEPLOY_PORT)" \
		|| echo "deployed, but status check failed — run 'make logs'"

logs:
	ssh $(DEPLOY_HOST) 'cd $(DEPLOY_DIR) && docker compose logs --tail 50 -f'

status:
	@curl -s http://$$(echo $(DEPLOY_HOST) | cut -d@ -f2):$(DEPLOY_PORT)/api/status | python3 -m json.tool
