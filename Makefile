.PHONY: install db run test lint format audit check

install:
	uv sync --all-groups

db:
	docker compose up -d db

run:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

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
