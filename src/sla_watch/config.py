"""Runtime configuration from environment variables (with safe defaults)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    db_path: Path = Path("data/warehouse.db")
    landing_dir: Path = Path("data/landing")
    window_days: int = 90
    overlap_hours: int = 2
    page_size: int = 50_000
    soda_base_url: str = "https://data.cityofnewyork.us"
    soda_dataset: str = "erm2-nwe9"
    soda_app_token: str | None = None

    @classmethod
    def from_env(cls) -> Config:
        env = os.environ
        return cls(
            db_path=Path(env.get("SLA_DB_PATH", "data/warehouse.db")),
            landing_dir=Path(env.get("SLA_LANDING_DIR", "data/landing")),
            window_days=int(env.get("SLA_WINDOW_DAYS", "90")),
            overlap_hours=int(env.get("SLA_OVERLAP_HOURS", "2")),
            page_size=int(env.get("SLA_PAGE_SIZE", "50000")),
            soda_base_url=env.get("SODA_BASE_URL", "https://data.cityofnewyork.us"),
            soda_dataset=env.get("SODA_DATASET", "erm2-nwe9"),
            soda_app_token=env.get("SODA_APP_TOKEN") or None,
        )
