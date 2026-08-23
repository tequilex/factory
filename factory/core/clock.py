"""Single source of time.

Everything stored in the database is UTC in ISO-8601 with a ``Z`` suffix, second
precision. That keeps timestamp strings sortable, which several queries rely on,
and means moving the server to another host never shifts a schedule. A project's
timezone is applied only at the edges: publishing windows and reports.

Tests control time by monkeypatching :func:`now_utc`, so no other module may call
``datetime.now`` directly.
"""

from __future__ import annotations

from datetime import datetime, timezone

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now_utc() -> datetime:
    """Current moment as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    """Render an aware datetime as the canonical UTC string."""
    if value.tzinfo is None:
        raise ValueError(
            "to_iso() требует datetime с часовым поясом. "
            "Наивное время нельзя записывать в базу: непонятно, в каком оно поясе."
        )
    return value.astimezone(timezone.utc).strftime(ISO_FORMAT)


def from_iso(value: str) -> datetime:
    """Parse a stored timestamp back into an aware UTC datetime."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
