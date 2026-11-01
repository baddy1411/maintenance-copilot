.PHONY: env db migrate test test-unit lint fmt down

# Adds any missing DB passwords to .env with random values. Values are never printed.
env:
	@test -f .env || cp .env.example .env
	@for k in POSTGRES_PASSWORD DB_PASSWORD_MIGRATOR DB_PASSWORD_APP_RW DB_PASSWORD_TOOLS_RO DB_PASSWORD_INGEST_RW DB_PASSWORD_MCP_RW USER_HASH_SALT; do \
	  if ! grep -q "^$$k=." .env; then \
	    sed -i.bak "/^$$k=$$/d" .env && rm -f .env.bak; \
	    echo "$$k=$$(openssl rand -hex 24)" >> .env; echo "added $$k"; \
	  fi; \
	done
	@grep -q '^DEV_JWT_KEY_FILE=.' .env || (sed -i.bak '/^DEV_JWT_KEY_FILE=$$/d' .env && rm -f .env.bak && echo "DEV_JWT_KEY_FILE=data/dev-jwt-key.pem" >> .env && echo "added DEV_JWT_KEY_FILE")
	@uv run copilot dev-keys

db: env
	docker compose up -d --wait db

migrate: db
	uv run copilot migrate

test:
	uv run pytest

test-unit:
	uv run pytest -m "not integration"

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

fmt:
	uv run ruff format .
	uv run ruff check --fix .

down:
	docker compose down
