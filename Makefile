# uv and Node 20+.

PY_VERSION := 3.14
UV         := uv
VENV       := .venv
RUN        := $(UV) run
SRC        := src/uparse
TESTS      := tests
BROWSER    := browser

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: setup
setup: venv browser-install ## Full first-time setup (python env + browser worker)
	@echo "==> setup complete. try: make check"

.PHONY: venv
venv: ## Create the virtualenv and install the package with dev extras
	$(UV) venv --python $(PY_VERSION)
	$(UV) pip install -e ".[dev]"

.PHONY: lock
lock: ## Refresh uv.lock
	$(UV) lock

.PHONY: sync
sync: ## Install exactly what the lockfile pins
	$(UV) sync --all-extras

.PHONY: fmt
fmt: ## Format Python (ruff) and the browser worker (tsc-adjacent tooling if present)
	$(RUN) ruff format $(SRC) $(TESTS)
	$(RUN) ruff check --fix $(SRC) $(TESTS)

.PHONY: lint
lint: ## Lint without modifying files
	$(RUN) ruff check $(SRC) $(TESTS)
	$(RUN) ruff format --check $(SRC) $(TESTS)

.PHONY: type
type: ## Static type check
	$(RUN) mypy

.PHONY: test
test: ## Run the test suite (skips browser-marked tests)
	$(RUN) pytest -m "not browser"

.PHONY: test-all
test-all: browser ## Run everything, including browser integration tests
	$(RUN) pytest

.PHONY: eval
eval: ## Score the extraction engine against the saved corpus: make eval ARGS=-v
	$(RUN) python tools/eval.py $(ARGS)

.PHONY: cov
cov: ## Test suite with coverage report
	$(RUN) pytest -m "not browser" --cov --cov-report=term-missing

.PHONY: check
check: lint type test ## Everything CI runs

.PHONY: browser-install
browser-install: ## Install worker deps + Chromium
	cd $(BROWSER) && npm install
	cd $(BROWSER) && npx playwright install chromium

.PHONY: browser
browser: ## Compile the TypeScript worker to browser/dist
	cd $(BROWSER) && npm run build

.PHONY: browser-check
browser-check: ## Type-check the worker without emitting
	cd $(BROWSER) && npm run typecheck

.PHONY: run
run: ## Run the CLI: make run ARGS="https://example.com --output out.json"
	$(RUN) uparse $(ARGS)

.PHONY: inspect
inspect: ## Inspect a URL: make inspect URL=https://example.com
	$(RUN) uparse inspect $(URL)

.PHONY: clean
clean: ## Remove build/test artifacts (keeps the venv and node_modules)
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	rm -rf $(BROWSER)/dist
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.egg-info' -type d -prune -exec rm -rf {} +

.PHONY: distclean
distclean: clean ## Also remove the venv and node_modules
	rm -rf $(VENV) $(BROWSER)/node_modules
