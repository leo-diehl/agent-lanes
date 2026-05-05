from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def iso_after(seconds: float) -> str:
    return (utc_now() + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def timestamp_slug() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%S%fZ")
