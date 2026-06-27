# Makefile — FastAPI Cloud commands wired to the PROJECT-LOCAL credentials.
#
# The auth token + app link live in ./.fastapicloud/ (gitignored), not in
# ~/.config. Every target below points the CLI there via
# FASTAPI_CLOUD_CLI_CONFIG_DIR, same as deploy.sh — so these work without a
# global `fastapi login`.
#
# Uses the project venv's pinned CLI (.venv/bin/fastapi), not whatever `fastapi`
# happens to be on PATH.
#
# Usage:
#   make deploy                       # deploy (blocks on build)
#   make deploy-no-wait               # deploy, don't wait for the build
#   make logs                         # stream live logs
#   make logs-recent                  # last 100 lines, then exit
#   make env-list                     # show registered env vars
#   make env-set NAME=FOO VALUE=bar   # set an env var
#   make env-set-secret NAME=FOO VALUE=bar   # set a secret env var
#   make env-get NAME=FOO             # read one env var
#   make env-delete NAME=FOO          # delete an env var
#   make whoami                       # who the local token is logged in as
#   make deployments                  # list deployments
#   make help                         # list all targets

# Resolve to this Makefile's directory so targets work from anywhere.
ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))

# Project-local FastAPI Cloud CLI: pinned venv binary + project-local auth dir.
export FASTAPI_CLOUD_CLI_CONFIG_DIR := $(ROOT)/.fastapicloud
FASTAPI := $(ROOT)/.venv/bin/fastapi
CLOUD   := $(FASTAPI) cloud

.DEFAULT_GOAL := help

.PHONY: help deploy deploy-no-wait logs logs-recent env-list env-get \
        env-set env-set-secret env-delete whoami apps deployments login

help: ## List available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

## ---- deploy ---------------------------------------------------------------

deploy: ## Deploy to FastAPI Cloud (blocks on the build)
	$(CLOUD) deploy "$(ROOT)"

deploy-no-wait: ## Deploy without waiting for the build to finish
	$(CLOUD) deploy --no-wait "$(ROOT)"

## ---- logs -----------------------------------------------------------------

logs: ## Stream live logs (Ctrl-C to stop)
	$(CLOUD) logs "$(ROOT)"

logs-recent: ## Fetch the last 100 log lines and exit (override: TAIL=, SINCE=)
	$(CLOUD) logs --no-follow --tail $(or $(TAIL),100) --since $(or $(SINCE),5m) "$(ROOT)"

## ---- environment variables ------------------------------------------------

env-list: ## List the app's registered env vars
	$(CLOUD) env list

env-get: ## Read one env var: make env-get NAME=DATABASE_URL
	@test -n "$(NAME)" || { echo "usage: make env-get NAME=KEY"; exit 1; }
	$(CLOUD) env get "$(NAME)"

env-set: ## Set an env var: make env-set NAME=KEY VALUE=val
	@test -n "$(NAME)" || { echo "usage: make env-set NAME=KEY VALUE=val"; exit 1; }
	$(CLOUD) env set "$(NAME)" "$(VALUE)"

env-set-secret: ## Set a SECRET env var: make env-set-secret NAME=KEY VALUE=val
	@test -n "$(NAME)" || { echo "usage: make env-set-secret NAME=KEY VALUE=val"; exit 1; }
	$(CLOUD) env set --secret "$(NAME)" "$(VALUE)"

env-delete: ## Delete an env var: make env-delete NAME=KEY
	@test -n "$(NAME)" || { echo "usage: make env-delete NAME=KEY"; exit 1; }
	$(CLOUD) env delete "$(NAME)"

## ---- account / app inspection ---------------------------------------------

whoami: ## Show the currently logged-in user (for the local token)
	$(CLOUD) whoami

apps: ## Manage / list your FastAPI Cloud apps
	$(CLOUD) apps

deployments: ## List deployments for this app
	$(CLOUD) deployments list

login: ## (Re)authenticate the project-local token
	$(CLOUD) login
