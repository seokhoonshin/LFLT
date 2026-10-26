# RouteOpt developer targets. PowerShell equivalents are in README.md ("Windows").
PY ?= python
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: venv install dev test lint typecheck format check run sample osrm-setup osrm-up osrm-down clean

venv:
	$(PY) -m venv $(VENV)

install: venv
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e .

dev: venv
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

test:
	$(BIN)/python -m pytest -q --cov=routeopt --cov-report=term-missing

lint:
	$(BIN)/ruff check routeopt app tests data

format:
	$(BIN)/ruff format routeopt app tests data

typecheck:
	$(BIN)/mypy routeopt

check: lint typecheck test

run:
	$(BIN)/streamlit run app/Home.py

sample:
	$(BIN)/routeopt sample --shipments 120

osrm-setup:
	cd docker/osrm && ./setup.sh

osrm-up:
	cd docker/osrm && docker compose up -d

osrm-down:
	cd docker/osrm && docker compose down

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
