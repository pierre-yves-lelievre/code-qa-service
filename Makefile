.PHONY: install run test lint format docker-build docker-run docker-stop

install:
	uv sync --all-groups

run:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .

docker-build:
	docker build -t surrogate-model-service .

docker-run:
	docker compose up --build -d

docker-stop:
	docker compose down
