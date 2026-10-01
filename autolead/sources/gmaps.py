"""Source Google Maps via l'API officielle Google Places (Text Search, "Places API (New)").

Requêtes "mot-clé + ville" -> établissements avec site web, téléphone, adresse (jusqu'à 60 par requête).
Clé : https://console.cloud.google.com/ (activer "Places API (New)") ; variable GOOGLE_MAPS_API_KEY
ou --google-key. Payant au-delà du quota gratuit mensuel : voir la grille tarifaire Google.
"""
from __future__ import annotations

import asyncio

import aiohttp

from ..config import CATEGORIES, refine_category
from ..db import DB
from ..utils import RateLimiter, log

API = "https://places.googleapis.com/v1/places:searchText"


class KeyRejected(Exception):
    """Clé refusée par Google (invalide, suspendue, API non activée, facturation) : inutile d'insister."""


FIELDS = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.addressComponents",
    "places.websiteUri", "places.nationalPhoneNumber", "places.location", "places.businessStatus",
    "nextPageToken",
])


def place_to_business(p: dict, category: str, country: str = "FR") -> dict | None:
    if p.get("businessStatus") == "CLOSED_PERMANENTLY":
        return None
    name = (p.get("displayName") or {}).get("text", "")
    comps = {t: c.get("longText", "") for c in p.get("addressComponents") or [] for t in c.get("types", [])}
    loc = p.get("location") or {}
    return {
        "source": "gmaps",
        "source_id": p.get("id"),
        "name": name,
        "category": refine_category(category, name),
        "address": p.get("formattedAddress", ""),
        "postal_code": comps.get("postal_code", ""),
        "city": comps.get("locality", ""),
        "country": country,
        "phone": p.get("nationalPhoneNumber", ""),
        "website": p.get("websiteUri", ""),
        "lat": loc.get("latitude"),
        "lon": loc.get("longitude"),
    }


async def _search(session, limiter, key: str, query: str, page_token: str | None, country: str) -> dict:
    body = {"textQuery": query, "languageCode": "fr", "regionCode": country.upper(), "pageSize": 20}
    if page_token:
        body["pageToken"] = page_token
    headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS, "Content-Type": "application/json"}
    for attempt in range(5):
        await limiter.wait()
        try:
            async with session.post(API, json=body, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                if r.status == 429 or r.status >= 500:
                    await asyncio.sleep(2 ** attempt)
                    continue
                text = await r.text()
                if r.status in (400, 401, 403):
                    raise KeyRejected(f"HTTP {r.status} : {text[:400]}")
                log(f"[gmaps] HTTP {r.status} pour « {query} » : {text[:200]}")
                return {}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            await asyncio.sleep(2 ** attempt)
    return {}


async def collect_gmaps(db: DB, session: aiohttp.ClientSession, categories: list[str], cities: list[str],
                        *, api_key: str, pages: int = 3, qps: float = 5.0, country: str = "FR") -> int:
    queries = [(cat, kw, city) for cat in categories for kw in CATEGORIES[cat]["keywords"] for city in cities]
    log(f"[gmaps] {len(queries)} requêtes x {pages} pages max ({qps}/s)")
    limiter = RateLimiter(qps)
    sem = asyncio.Semaphore(8)
    total = 0

    async def one(cat: str, kw: str, city: str) -> None:
        nonlocal total
        token = None
        async with sem:
            for _ in range(pages):
                data = await _search(session, limiter, api_key, f"{kw} {city}", token, country)
                items = [b for p in data.get("places") or [] if (b := place_to_business(p, cat, country))]
                total += db.add_businesses(items)
                token = data.get("nextPageToken")
                if not token:
                    break

    tasks = [asyncio.ensure_future(one(*q)) for q in queries]
    try:
        await asyncio.gather(*tasks)
    except KeyRejected as exc:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log(f"[gmaps] arrêt : clé Google refusée -> {exc}")
        log("[gmaps] vérifiez la clé, l'activation de « Places API (New) » et la facturation du projet.")
        log("[gmaps] les autres sources continuent normalement.")
    log(f"[gmaps] {total} nouveaux établissements")
    return total
