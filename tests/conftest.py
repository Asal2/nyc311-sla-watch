from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads((FIXTURES / name).read_text())
    return data


@pytest.fixture
def initial_batch() -> list[dict[str, Any]]:
    return load_fixture("batch_initial.json")


@pytest.fixture
def update_batch() -> list[dict[str, Any]]:
    return load_fixture("batch_updates.json")
