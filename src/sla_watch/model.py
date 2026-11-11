"""Row contract for 311 records. Invalid rows are quarantined, never silently dropped."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# Source columns we keep (Socrata name -> warehouse column).
COLUMNS: dict[str, str] = {
    "unique_key": "unique_key",
    "created_date": "created_date",
    "closed_date": "closed_date",
    "agency": "agency",
    "agency_name": "agency_name",
    "complaint_type": "complaint_type",
    "descriptor": "descriptor",
    "status": "status",
    "resolution_action_updated_date": "resolution_updated_date",
    "borough": "borough",
    "community_board": "community_board",
    "incident_zip": "incident_zip",
    "open_data_channel_type": "channel",
    "latitude": "latitude",
    "longitude": "longitude",
}
REQUIRED_SOURCE_COLUMNS = {"unique_key", "created_date", "agency", "complaint_type", "status"}


def _parse_floating(value: str | None) -> str | None:
    """Validate a Socrata floating timestamp and normalise to 'YYYY-MM-DDTHH:MM:SS'."""
    if value is None or value == "":
        return None
    return datetime.fromisoformat(value.replace("Z", "")).strftime("%Y-%m-%dT%H:%M:%S")


class Request311(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    socrata_id: str = Field(alias=":id", min_length=1)
    source_updated_at: str = Field(alias=":updated_at", min_length=1)
    unique_key: str = Field(pattern=r"^\d+$")
    created_date: str
    closed_date: str | None = None
    agency: str = Field(min_length=1)
    agency_name: str | None = None
    complaint_type: str = Field(min_length=1)
    descriptor: str | None = None
    status: str = Field(min_length=1)
    resolution_updated_date: str | None = Field(
        default=None, alias="resolution_action_updated_date"
    )
    borough: str | None = None
    community_board: str | None = None
    incident_zip: str | None = None
    channel: str | None = Field(default=None, alias="open_data_channel_type")
    latitude: float | None = None
    longitude: float | None = None

    @field_validator("created_date", mode="before")
    @classmethod
    def _created(cls, v: Any) -> str:
        parsed = _parse_floating(v)
        if parsed is None:
            raise ValueError("created_date is required")
        return parsed

    @field_validator("closed_date", "resolution_updated_date", mode="before")
    @classmethod
    def _optional_ts(cls, v: Any) -> str | None:
        return _parse_floating(v)

    @field_validator("latitude", "longitude", mode="before")
    @classmethod
    def _blank_float(cls, v: Any) -> Any:
        return None if v in ("", None) else v

    def row_hash(self) -> str:
        """Hash of business content (excludes Socrata system fields)."""
        content = self.model_dump(exclude={"socrata_id", "source_updated_at"})
        blob = json.dumps(content, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()


def validate(raw: dict[str, Any]) -> tuple[Request311 | None, str | None]:
    try:
        return Request311.model_validate(raw), None
    except ValidationError as exc:
        reasons = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        return None, reasons
