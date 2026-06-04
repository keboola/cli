.DEFAULT_GOAL := help

.PHONY: help install install-mcp install-server sync test test-unit test-integration test-e2e test-e2e-local test-e2e-invite test-e2e-feature test-e2e-stream test-file test-cov lint lint-fix format format-check typecheck typecheck-warn skill-check skill-gen version-sync version-check changelog changelog-check check-error-codes command-sync-check check clean hooks web-install web-dev-backend web-dev-frontend web-build web-clean

help: ## Show this help message
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install in development mode (editable)
	uv pip install -e ".[dev]"

install-mcp: ## Install Keboola MCP server (required for 'tool' commands)
	uv pip install keboola-mcp-server

install-server: ## Install FastAPI/uvicorn for `kbagent serve` (web UI backend)
	uv pip install -e ".[server]"

sync: ## Sync dependencies from lockfile
	uv sync

test: ## Run all tests (excluding e2e — use test-e2e separately)
	uv run pytest tests/ -v -m "not e2e"

test-unit: ## Run unit tests only (exclude integration and e2e)
	uv run pytest tests/ -v -m "not integration and not e2e"

test-integration: ## Run integration tests only
	uv run pytest tests/ -v -m integration

test-e2e: ## Run E2E tests (E2E_API_TOKEN and E2E_URL required)
	uv run pytest tests/test_e2e.py tests/test_server_semantic_layer_routes_e2e.py -v -s --tb=long

test-e2e-local: ## Run E2E against a project in a local config.json (CONFIG_DIR=/path/.kbagent ALIAS=my-proj)
	KBAGENT_E2E_CONFIG_DIR=$(CONFIG_DIR) KBAGENT_E2E_ALIAS=$(ALIAS) \
		uv run pytest tests/test_e2e.py -v -s --tb=long

test-e2e-invite: ## Run project invite E2E (E2E_MANAGE_TOKEN + E2E_INVITE_PROJECT_ID required)
	uv run pytest tests/test_e2e.py -v -s --tb=long -m e2e_invite

test-e2e-feature: ## Run feature-flag E2E (E2E_MANAGE_TOKEN super-admin + E2E_API_TOKEN + E2E_URL required)
	uv run pytest tests/test_e2e.py -v -s --tb=long -k test_feature_flags_read_e2e

test-e2e-stream: ## Run Data Streams OTLP E2E (E2E_API_TOKEN + E2E_URL required; creates + deletes a temp source)
	uv run pytest tests/test_e2e.py -v -s --tb=long -k test_stream_otlp_e2e

test-file: ## Run a specific test file (FILE=tests/test_cli.py)
	uv run pytest $(FILE) -v

test-cov: ## Run the unit suite with a coverage report (informational; no threshold gate)
	uv run pytest tests/ -v -m "not integration" --cov --cov-report=term-missing

lint: ## Run ruff linter
	uv run ruff check src/ tests/ scripts/

lint-fix: ## Run ruff linter with auto-fix
	uv run ruff check src/ tests/ scripts/ --fix

format: ## Format code with ruff
	uv run ruff format .

format-check: ## Check code formatting (no changes)
	uv run ruff format . --check

typecheck: ## Run ty type-checker (Astral). Fails on any error.
	uv run ty check

typecheck-warn: ## Run ty in warning-only mode (always exits 0; used by hooks)
	@uv run ty check || true

skill-gen: ## Regenerate SKILL.md from CLI command tree
	uv run python scripts/generate_skill.py

skill-check: ## Check SKILL.md is up-to-date (fails if stale)
	@uv run python scripts/generate_skill.py > /dev/null 2>&1
	@if git diff --quiet plugins/kbagent/skills/kbagent/SKILL.md; then \
		echo "SKILL.md is up-to-date"; \
	else \
		echo "ERROR: SKILL.md is out-of-date. Run 'make skill-gen' and commit."; \
		git diff plugins/kbagent/skills/kbagent/SKILL.md; \
		exit 1; \
	fi

version-sync: ## Sync version from pyproject.toml to plugin.json
	uv run python scripts/sync_version.py

version-check: ## Check version-bearing files match pyproject.toml (fails if mismatched)
	@uv run python scripts/sync_version.py > /dev/null 2>&1
	@if git diff --quiet plugins/kbagent/.claude-plugin/plugin.json .claude-plugin/marketplace.json uv.lock; then \
		echo "version is in sync (plugin.json, marketplace.json, uv.lock)"; \
	else \
		echo "ERROR: version mismatch. Run 'make version-sync' and commit."; \
		git diff plugins/kbagent/.claude-plugin/plugin.json .claude-plugin/marketplace.json uv.lock; \
		exit 1; \
	fi

changelog: ## Generate changelog skeleton from GitHub releases
	uv run python scripts/generate_changelog.py

changelog-check: ## Check all releases have changelog entries
	uv run python scripts/generate_changelog.py --check

check-error-codes: ## Reject raw error_code string literals in source (use ErrorCode enum)
	uv run python scripts/check_error_codes.py

command-sync-check: ## Verify every CLI command is registered + documented (silent-drift gate)
	uv run python scripts/check_command_sync.py

hooks: ## Install git pre-commit hook (lint + format on staged files)
	cp scripts/pre-commit .git/hooks/pre-commit
	chmod +x .git/hooks/pre-commit
	@echo "Pre-commit hook installed."

check: lint format-check typecheck skill-check version-check command-sync-check changelog-check check-error-codes test ## Run all checks (lint + format + typecheck + skill + version + command-sync + changelog + error-codes + test)

clean: ## Remove build artifacts and caches
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true

# ── Web UI (web/backend Node BFF + web/frontend React) ─────────────

web-install: ## Install web/backend + web/frontend npm dependencies
	cd web/backend && npm install
	cd web/frontend && npm install

web-dev: ## Spin up kbagent serve + BFF + Vite in ONE terminal (Ctrl+C kills all)
	./scripts/web-dev.sh $(if $(CONFIG_DIR),--config-dir $(CONFIG_DIR),)

web-dev-backend: ## Run only the Node BFF in watch mode (needs KBAGENT_SERVE_TOKEN env)
	cd web/backend && npm run dev

web-dev-frontend: ## Run only the Vite dev server (proxies /api -> BFF on :8000)
	cd web/frontend && npm run dev

web-build: ## Build the React app into web/frontend/dist
	cd web/frontend && npm run build

web-clean: ## Remove web/* build artifacts and node_modules
	rm -rf web/frontend/dist web/frontend/node_modules
	rm -rf web/backend/dist web/backend/node_modules
