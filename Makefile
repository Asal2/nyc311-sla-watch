PY ?= python3
export PYTHONPATH := src

.PHONY: setup lint typecheck test-unit test-integration test check run status

setup:            ## install pinned deps + package (CI / fresh machines)
	$(PY) -m pip install -r requirements-dev.lock
	$(PY) -m pip install --no-deps -e .

lint:
	ruff check src tests
	ruff format --check src tests

typecheck:
	mypy src

test-unit:
	$(PY) -m pytest -q tests/unit

test-integration:
	$(PY) -m pytest -q tests/integration

test: test-unit test-integration

check: lint typecheck test   ## everything CI runs

run:              ## incremental extract + load against the live API
	$(PY) -m sla_watch.cli extract-load

status:
	$(PY) -m sla_watch.cli status
