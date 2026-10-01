"""Session HTTP asynchrone partagée (connexions réutilisées, cache DNS)."""
from __future__ import annotations

import aiohttp

DEFAULT_UA = "Mozilla/5.0 (compatible; AutoLeadBot/1.0)"


def make_session(
    *,
    concurrency: int = 100,
    per_host: int = 4,
    timeout: float = 15.0,
    user_agent: str = DEFAULT_UA,
    trust_env: bool = True,
    headers: dict | None = None,
) -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(limit=concurrency, limit_per_host=per_host, ttl_dns_cache=900)
    h = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.6",
    }
    if headers:
        h.update(headers)
    return aiohttp.ClientSession(
        connector=connector,
        headers=h,
        timeout=aiohttp.ClientTimeout(total=timeout, connect=min(8.0, timeout)),
        trust_env=trust_env,  # respecte HTTP(S)_PROXY si défini
        cookie_jar=aiohttp.DummyCookieJar(),
        max_line_size=32768,
        max_field_size=32768,
    )
