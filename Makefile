# Agentic Browser — operations Makefile.
#
# Thin wrapper over infra/ (Docker self-host + the local dev script). All Docker
# files live in infra/; the build context is the repo root (this directory), and
# .dockerignore stays here. Run `make help` for the full list.

COMPOSE     := docker compose -f infra/docker-compose.yml
RUN_SH      := infra/run.sh
SCREEN_NAME := agenticemirates

.DEFAULT_GOAL := help

# ---- local development (uvicorn + Postgres via infra/run.sh) -----------------
# run/dev/backend launch the stack in a detached screen "$(SCREEN_NAME)" and
# return the prompt; use `make attach` to view it, `make stop` to kill it. Add
# NO_SCREEN=1 (e.g. `make run NO_SCREEN=1`) to run inline in the current terminal.

.PHONY: run
run: ## Local dev in screen: Postgres + backend + built UI on one port
	$(RUN_SH)

.PHONY: dev
dev: ## Local dev in screen with hot-reload Vite UI (DEV=1)
	DEV=1 $(RUN_SH)

.PHONY: backend
backend: ## Local dev in screen, backend + Postgres only (no frontend build)
	BACKEND_ONLY=1 $(RUN_SH)

.PHONY: attach
attach: ## Attach to the running dev screen (Ctrl-A D to detach)
	screen -r $(SCREEN_NAME)

.PHONY: stop
stop: ## Stop the dev screen (the backgrounded app)
	-screen -S $(SCREEN_NAME) -X quit

# ---- Docker self-host (the full image) ---------------------------------------

.PHONY: up
up: ## Build + start the full self-host stack (app + Postgres) in Docker
	$(COMPOSE) up --build

.PHONY: up-d
up-d: ## Same as `up` but detached
	$(COMPOSE) up --build -d

.PHONY: build
build: ## Build the Docker image only
	$(COMPOSE) build

.PHONY: down
down: ## Stop and remove the self-host stack containers
	$(COMPOSE) down

.PHONY: db
db: ## Start only the Postgres service (what local dev uses)
	$(COMPOSE) up -d postgres

.PHONY: logs
logs: ## Tail logs from the running stack
	$(COMPOSE) logs -f

.PHONY: ps
ps: ## Show the status of the stack services
	$(COMPOSE) ps

.PHONY: config
config: ## Validate + render the resolved compose config
	$(COMPOSE) config

.PHONY: clean
clean: ## Stop the stack and delete its volumes (DROPS the Postgres data)
	$(COMPOSE) down -v

# ---- meta --------------------------------------------------------------------

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
