"""Source moteur de recherche via l'API officielle Brave Search (clé gratuite/payante).

Requêtes "mot-clé + ville" -> sites web d'entreprises, ensuite crawlés.
Clé : https://brave.com/search/api/  (variable d'environnement BRAVE_API_KEY ou --brave-key)
"""
from __future__ import annotations

import asyncio

import aiohttp

from ..config import CATEGORIES
from ..db import DB
from ..utils import RateLimiter, log, site_from_url

API = "https://api.search.brave.com/res/v1/web/search"


async def _search(session, limiter, key: str, query: str, offset: int, country: str) -> list[dict]:
    params = {"q": query, "count": 20, "offset": offset, "country": country.lower(),
              "search_lang": "fr", "result_filter": "web"}
    headers = {"Accept": "application/json", "X-Subscription-Token": key}
    for attempt in range(5):
        await limiter.wait()
        try:
            async with session.get(API, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    return (data.get("web") or {}).get("results") or []
                if r.status == 429 or r.status >= 500:
                    await asyncio.sleep(2 ** attempt)
                    continue
                log(f"[search] HTTP {r.status} pour « {query} » : {(await r.text())[:150]}")
                return []
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            await asyncio.sleep(2 ** attempt)
    return []


async def collect_search(db: DB, session: aiohttp.ClientSession, categories: list[str], cities: list[str],
                         *, api_key: str, pages: int = 1, qps: float = 1.0, country: str = "FR",
                         keywords: list[str] | None = None) -> int:
    if keywords:
        queries = [("autre", kw, city) for kw in keywords for city in cities]
    else:
        queries = [(cat, kw, city) for cat in categories for kw in CATEGORIES[cat]["keywords"] for city in cities]
    pages = max(1, min(pages, 10))  # l'API Brave donne au plus 10 pages de 20 résultats
    log(f"[search] {len(queries) * pages} requêtes ({qps}/s)")
    limiter = RateLimiter(qps)
    total = 0

    async def one(cat: str, kw: str, city: str) -> None:
        nonlocal total
        items = []
        for page in range(pages):
            for res in await _search(session, limiter, api_key, f"{kw} {city}", page, country):
                key, _ = site_from_url(res.get("url"))
                if not key:
                    continue
                host_root = res["url"].split("/", 3)
                items.append({
                    "source": "search", "source_id": key, "name": res.get("title", "")[:200],
                    "category": cat, "city": city, "country": country,
                    "website": "/".join(host_root[:3]) + "/",
                })
        total += db.add_businesses(items)

    sem = asyncio.Semaphore(4)

    async def guarded(q):
        async with sem:
            await one(*q)

    await asyncio.gather(*(guarded(q) for q in queries))
    log(f"[search] {total} nouveaux sites")
    return total
