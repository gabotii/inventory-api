# Convenience targets (Linux/macOS). Windows equivalents are in README.md.
PY ?= .venv/bin/python

.PHONY: install lint typecheck test test-unit migrate run smoke lock audit docker-up docker-down

install:
	python3.12 -m venv .venv
	$(PY) -m pip install --require-hashes -r requirements/dev.txt

lint:
	$(PY) -m ruff format --check .
	$(PY) -m ruff check .

typecheck:
	$(PY) -m mypy

test-unit:
	$(PY) -m pytest tests/unit

test:
	$(PY) -m pytest --cov --cov-report=term

migrate:
	$(PY) -m alembic upgrade head

run:
	$(PY) -m uvicorn app.main:app_factory --factory --reload --port 8000

smoke:
	$(PY) scripts/smoke_test.py

lock:
	$(PY) -m uv pip compile pyproject.toml --universal --python-version 3.12 --generate-hashes -o requirements/prod.txt
	$(PY) -m uv pip compile pyproject.toml --group dev --universal --python-version 3.12 --generate-hashes -o requirements/dev.txt

audit:
	$(PY) -m uv tool run pip-audit --require-hashes --disable-pip -r requirements/prod.txt

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down
