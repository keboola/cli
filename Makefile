.DEFAULT_GOAL := help

.PHONY: help install install-mcp sync test test-unit test-integration test-file lint lint-fix format format-check skill-check skill-gen check clean

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install in development mode (editable)
	uv pip install -e ".[dev]"

install-mcp: ## Install Keboola MCP server (required for 'tool' commands)
	uv pip install keboola-mcp-server

sync: ## Sync dependencies from lockfile
	uv sync

test: ## Run all tests
	uv run pytest tests/ -v

test-unit: ## Run unit tests only (exclude integration)
	uv run pytest tests/ -v -m "not integration"

test-integration: ## Run integration tests only
	uv run pytest tests/ -v -m integration

test-file: ## Run a specific test file (FILE=tests/test_cli.py)
	uv run pytest $(FILE) -v

lint: ## Run ruff linter
	uv run ruff check src/ tests/ scripts/

lint-fix: ## Run ruff linter with auto-fix
	uv run ruff check src/ tests/ scripts/ --fix

format: ## Format code with ruff
	uv run ruff format .

format-check: ## Check code formatting (no changes)
	uv run ruff format . --check

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

check: lint format-check skill-check test ## Run all checks (lint + format + skill + test)

clean: ## Remove build artifacts and caches
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
