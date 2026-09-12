.PHONY: install db run test lint format audit check eval smoke mcp web

install:
	uv sync --all-groups

db:
	docker compose up -d --wait db
	docker compose exec -T db psql -U postgres -v ON_ERROR_STOP=1 \
		-f /docker-entrypoint-initdb.d/10-codeqa-test.sql

# Watch app/ only: clones land in data/clones, and a reload there would stall every request
# until the running index job finishes.
run:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir app

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .

audit:
	uv export --frozen --all-groups --no-emit-project --format requirements-txt \
		| uv run pip-audit --strict --disable-pip --require-hashes -r /dev/stdin

check: lint
	uv run ruff format --check .
	uv run pytest

# Spends money with PROVIDERS=real; `make eval ARGS=--reindex` forces a re-index.
eval:
	uv run python -m evals.run_evals $(ARGS)

# Spends money with PROVIDERS=real (about $0.07 on pallets/markupsafe); `ARGS=--repo URL` for another.
smoke:
	uv run python -m evals.smoke $(ARGS)

# The MCP server over stdio; it calls the running service at settings.service_url.
mcp:
	uv run python -m app.mcp_server

# Type-checks and builds the page into app/static, which the app serves at /.
web:
	cd web && npm ci && npm run build
