.PHONY: install dev test lint fmt

install:
	python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

dev:
	.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8001

test:
	.venv/bin/pytest -v

lint:
	.venv/bin/ruff check app tests

fmt:
	.venv/bin/ruff format app tests
