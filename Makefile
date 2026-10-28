.PHONY: check test lint

check: lint test

lint:
	ruff check .

test:
	python3 -m pytest -q tests
