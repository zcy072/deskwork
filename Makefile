.DEFAULT_GOAL := help

SHELL := /bin/bash

.PHONY: help backend-lint backend-test frontend-test frontend-build check compose-health sandbox-verify

help:
	@printf '%s\n' \
	  'make backend-lint    Run Ruff checks and formatting validation' \
	  'make backend-test    Run the backend pytest suite' \
	  'make frontend-test   Run frontend unit tests' \
	  'make frontend-build  Type-check and build the frontend' \
	  'make check           Run local static checks and frontend verification' \
	  'make compose-health  Check the running backend health endpoint' \
	  'make sandbox-verify  Run the real sandbox lifecycle acceptance script'

backend-lint:
	cd backend && uv run ruff check app tests && uv run ruff format --check app tests

backend-test:
	cd backend && uv run pytest -q

frontend-test:
	cd frontend && npm test

frontend-build:
	cd frontend && npm run build

# Keep the default check independent of Docker and external model services.
check: backend-lint frontend-test frontend-build

compose-health:
	curl --noproxy '*' --fail --silent --show-error http://127.0.0.1:5001/health
	@printf '\n'

sandbox-verify:
	docker compose exec -T backend python - < scripts/verify_sandbox.py
