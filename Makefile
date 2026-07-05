# Makefile — Image Build Pipeline (SS1)
#
# Targets:
#   make build-image  — bake the Talos template into Proxmox (needs .env or env vars)
#   make clean-image  — remove build/image-id.txt so the next build is fresh
#   make test         — run pytest with coverage
#   make lint         — run ruff + mypy

SHELL := /bin/bash
PYTHON ?= python

# ---------------------------------------------------------------------------
# Required env (resolved at runtime, never echoed)
# ---------------------------------------------------------------------------

TALOS_VERSION ?= v1.10.0

# PVE endpoint / node / token — sourced from .env if present.
-include .env
export

# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

.PHONY: build-image clean-image test lint install-deps

build-image:
	@if [[ -z "$$PVE_ENDPOINT" || -z "$$PVE_TOKEN_ID" || -z "$$PVE_TOKEN_SECRET" ]]; then \
		echo "ERROR: set PVE_ENDPOINT, PVE_TOKEN_ID, PVE_TOKEN_SECRET in .env or env vars" >&2; \
		exit 2; \
	fi
	@$(PYTHON) tools/build_image.py \
		--talos-version $(TALOS_VERSION) \
		--pve-endpoint $$PVE_ENDPOINT \
		--pve-node "$${PVE_NODE:-bigbertha}" \
		--pve-token-id "$$PVE_TOKEN_ID" \
		--pve-token-secret "$$PVE_TOKEN_SECRET"

clean-image:
	@rm -f build/image-id.txt build/.build.lock
	@echo "build/ state cleared"

test:
	@$(PYTHON) -m pytest tools/tests/ -v

lint:
	@$(PYTHON) -m ruff check tools/
	@$(PYTHON) -m mypy tools/

install-deps:
	@$(PYTHON) -m pip install --user pytest pyyaml mypy ruff types-PyYAML