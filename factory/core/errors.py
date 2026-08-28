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
    """An external provider (LLM, images, publisher) failed or is misconfigured.

    Carries three optional facts that the retry machinery needs but that would be
    lost if the provider only raised prose:

    * ``status_code`` — so a 429 or a 502 is recognised as worth retrying. A
      provider that swallows the code into a message turns a transient blip into
      a burnt attempt;
    * ``retry_after`` — seconds the server asked us to wait;
    * ``delivered_unknown`` — whether the request may have arrived despite the
      failure. A connection that was never established proves nothing arrived,
      and the caller may safely try again. A read timeout proves nothing at all:
      the request went out, and a repeat may duplicate whatever it did;
    * ``cost`` — money already spent on a call that then failed. The model
      charges for a reply that does not parse just as it charges for one that
      does; leaving this out makes the spend report understate exactly when the
      owner most needs it;
    * ``needs_human`` — only a person can lift this refusal. An exhausted
      spending limit on a key comes back as an ordinary 429, indistinguishable
      by code from "you are calling too often" — but no amount of waiting clears
      it. Retrying such a refusal burns the post's attempts on an event that
      cannot happen, and the post dies overnight while the owner sleeps. The
      state machine turns this into ``WAITING`` plus one alert, the same way it
      treats an expired VK key.
    """

    def __init__(
        self,
        what: str,
        *,
        why: str | None = None,
        what_to_do: str | None = None,
        status_code: int | None = None,
        retry_after: float | None = None,
        cost: float | None = None,
        delivered_unknown: bool = False,
        needs_human: bool = False,
    ) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        self.cost = cost
        self.delivered_unknown = delivered_unknown
        self.needs_human = needs_human
        super().__init__(what, why=why, what_to_do=what_to_do)


class LockError(FactoryError):
    """Tick lock could not be acquired or released."""
