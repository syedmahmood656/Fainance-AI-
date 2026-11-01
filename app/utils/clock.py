from datetime import datetime, timezone


def utcnow() -> datetime:
    """Naive UTC 'now' (what our DateTime columns store) without the deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
