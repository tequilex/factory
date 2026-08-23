"""The only place an HTTP client is created.

Proxying is per provider, not global, and that is the whole reason this module
exists. From a Russian IP, OpenAI and Replicate need a proxy while VK must be
reached directly; from a foreign host it is the other way round. A single
``HTTPS_PROXY`` cannot express that.

Resolution order for provider ``llm``:

1. ``proxy_env`` named in the project config, if the config names one;
2. ``LLM_PROXY``;
3. ``HTTPS_PROXY`` / ``HTTP_PROXY``;
4. no proxy.

Creating ``httpx.Client()`` anywhere else means losing the proxy, the timeouts
and the redaction — the tests forbid it.
"""

from __future__ import annotations

import os

import httpx

from factory.core.logging import get_logger

# SPEC.md: connect 10s, read 120s. Image generation is slow; a short read timeout
# would abort calls that were about to succeed and get billed anyway.
CONNECT_TIMEOUT_SEC = 10.0
READ_TIMEOUT_SEC = 120.0

USER_AGENT = "factory/0.1 (+https://github.com/)"

log = get_logger(__name__)


def proxy_for(provider: str, *, proxy_env: str | None = None) -> str | None:
    """Which proxy URL a provider should use, or ``None`` for a direct connection."""
    candidates = []
    if proxy_env:
        candidates.append(proxy_env)
    candidates += [f"{provider.upper()}_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"]

    for name in candidates:
        value = os.environ.get(name)
        if value:
            return value
    return None


def client_for(
    provider: str,
    *,
    base_url: str | None = None,
    headers: dict[str, str] | None = None,
    proxy_env: str | None = None,
    timeout: httpx.Timeout | None = None,
) -> httpx.Client:
    """Build the HTTP client for one provider.

    ``base_url`` is always configurable: the owner may be going through a reseller
    or a mirror rather than the vendor's own endpoint.
    """
    proxy = proxy_for(provider, proxy_env=proxy_env)

    merged = {"User-Agent": USER_AGENT}
    if headers:
        merged.update(headers)

    if proxy:
        # The URL itself may carry credentials, so only the provider is logged.
        log.debug("клиент создан через прокси", extra={"provider": provider})

    return httpx.Client(
        base_url=base_url or "",
        headers=merged,
        proxy=proxy,
        timeout=timeout or httpx.Timeout(READ_TIMEOUT_SEC, connect=CONNECT_TIMEOUT_SEC),
        follow_redirects=True,
    )
