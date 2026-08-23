"""Human-readable errors.

The project owner does not read source code, so every error message is a piece of
documentation: what broke, why, and what to do about it. Never raise a bare
built-in exception at a boundary the owner can reach.
"""

from __future__ import annotations


class FactoryError(Exception):
    """Base error carrying three parts: what broke, why, what to do."""

    def __init__(
        self,
        what: str,
        *,
        why: str | None = None,
        what_to_do: str | None = None,
    ) -> None:
        self.what = what
        self.why = why
        self.what_to_do = what_to_do
        super().__init__(self.render())

    def render(self) -> str:
        lines = [self.what]
        if self.why:
            lines.append(f"Причина: {self.why}")
        if self.what_to_do:
            lines.append(f"Что делать: {self.what_to_do}")
        return "\n".join(lines)


class ConfigError(FactoryError):
    """Project config is missing, malformed, or points at something absent."""


class DbError(FactoryError):
    """Database file, migrations, or a query failed."""


class ProviderError(FactoryError):
    """An external provider (LLM, images, publisher) failed or is misconfigured."""


class LockError(FactoryError):
    """Tick lock could not be acquired or released."""
