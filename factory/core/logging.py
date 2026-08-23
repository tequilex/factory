"""Structured JSON logging on stdout, with two independent secret filters.

Docker's json-file driver collects stdout, so one JSON object per line is all the
plumbing needed. Cyrillic is written as-is: escaped ``\\uXXXX`` output would make
the logs unreadable for the owner, who is the person actually reading them.

Secrets are stripped twice, because either filter alone has a blind spot:

1. by field name — ``extra={"vk_token": ...}`` never reaches the output;
2. by value — anything matching a secret-looking environment variable is scrubbed
   from the rendered text, which catches ``log.info(f"token={t}")``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any, TextIO

from factory.core.clock import now_utc, to_iso

REDACTED = "***"

# Substring match. "idem_key" deliberately does not match: it is an identifier,
# not a credential, and it is genuinely useful in logs.
_SECRET_NAME_RE = re.compile(
    r"(token|secret|password|passwd|credential|authorization|api[_-]?key)",
    re.IGNORECASE,
)

# Exact match, for fields named plainly "key".
_SECRET_NAME_EXACT = {"key", "apikey"}

# Environment variables whose values must never appear in output.
_SECRET_ENV_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_KEY|APIKEY)",
    re.IGNORECASE,
)

# Below this length a value is not a credential, and scrubbing it would eat
# ordinary words out of every message.
_MIN_SECRET_LENGTH = 8

_RESERVED_RECORD_FIELDS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)


def _is_secret_name(name: str) -> bool:
    return name.lower() in _SECRET_NAME_EXACT or bool(_SECRET_NAME_RE.search(name))


def _redact_by_name(value: Any) -> Any:
    """Walk dicts and lists, blanking values whose key looks like a credential."""
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_secret_name(str(key)) else _redact_by_name(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_by_name(item) for item in value]
    return value


def _secret_values() -> list[str]:
    return [
        value
        for name, value in os.environ.items()
        if _SECRET_ENV_RE.search(name) and value and len(value) >= _MIN_SECRET_LENGTH
    ]


def scrub_text(text: str) -> str:
    """Remove any known secret value that leaked into free-form text."""
    for secret in _secret_values():
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": to_iso(now_utc()),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_FIELDS or key.startswith("_"):
                continue
            payload[key] = REDACTED if _is_secret_name(key) else _redact_by_name(value)

        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)

        rendered = json.dumps(payload, ensure_ascii=False, default=str)
        return scrub_text(rendered)


class _SafeExtras(logging.LoggerAdapter):
    """Keeps ``extra`` from colliding with LogRecord's own attributes.

    ``logging`` raises ``KeyError: Attempt to overwrite 'created' in LogRecord``
    when an extra field is named like a built-in one — and ``created``,
    ``module``, ``name``, ``args``, ``filename`` are all names a caller would
    naturally reach for. Worse, the crash only happens when the level is actually
    enabled, so it hides during development and takes the worker down in
    production. Colliding names get an ``f_`` prefix instead.
    """

    def process(self, msg, kwargs):
        extra = kwargs.get("extra")
        if extra:
            kwargs["extra"] = {
                (f"f_{key}" if key in _RESERVED_RECORD_FIELDS or key in {"message", "asctime"} else key): value
                for key, value in extra.items()
            }
        return msg, kwargs


def setup_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    """Install the JSON handler on the root logger, replacing any previous one.

    Idempotent: the CLI and the worker both call it, and duplicated handlers
    would double every line.
    """
    root = logging.getLogger()
    root.handlers.clear()

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.LoggerAdapter:
    return _SafeExtras(logging.getLogger(name), {})
