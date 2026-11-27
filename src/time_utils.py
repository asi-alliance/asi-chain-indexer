"""UTC timestamps compatible with existing naive database and API values."""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return naive UTC without the deprecated datetime.utcnow() API."""
    return datetime.now(UTC).replace(tzinfo=None)
