"""Source OpenStreetMap (API Overpass) : garages, concessions, CT... avec site web, e-mail, téléphone.

Données ouvertes (licence ODbL) : citer "© les contributeurs d'OpenStreetMap" si vous les réutilisez.
"""
from __future__ import annotations

import asyncio
import re

import aiohttp

from ..config import CATEGORIES, DEPARTEMENTS, refine_category
from ..db import DB
from ..utils import log

ENDPOINTS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]


# overpass-api.de répond 406 si on ne demande pas explicitement du JSON avec un User-Agent identifiable
_HEADERS = {"Accept": "application/json, */*;q=0.5",
            "User-Agent": "AutoLead/1.0 (+https://github.com/blacgoku991/lead)"}


def area_country(cc: str) -> str:
    return f'area["ISO3166-1"="{cc.upper()}"]["admin_level"="2"]->.a;'


def area_departement(dep: str) -> str:
    return f'area["boundary"="administrative"]["admin_level"="6"]["ref:INSEE"="{dep}"]->.a;'


def area_custom(expr: str) -> str:
    expr = expr.strip().rstrip(";")
    if expr.endswith("->.a"):
        expr = expr[: -len("->.a")]
    return f"{expr}->.a;"


def build_query(area_stmt: str, selectors: list[tuple[str, str]], timeout: int = 900) -> str:
    body = "\n".join(f'  nwr["{k}"="{v}"](area.a);' for k, v in selectors)
    return f"[out:json][timeout:{timeout}];\n{area_stmt}\n(\n{body}\n);\nout center tags;"


def selector_index(categories: list[str]) -> dict[tuple[str, str], str]:
    index: dict[tuple[str, str], str] = {}
    for cat in categories:
        for sel in CATEGORIES[cat]["osm"]:
            index.setdefault(tuple(sel), cat)
    return index


def element_to_business(el: dict, index: dict, country: str = "FR") -> dict | None:
    tags = el.get("tags") or {}
    cat = next((c for (k, v), c in index.items() if tags.get(k) == v), None)
    if cat is None:
        return None
    name = tags.get("name") or tags.get("brand") or tags.get("operator") or ""
    website = tags.get("website") or tags.get("contact:website") or tags.get("url") or ""
    raw_mail = tags.get("email") or tags.get("contact:email") or ""
    emails = [e for e in re.split(r"[;,\s]+", raw_mail) if "@" in e]
    if not (name or website or emails):
        return None
    center = el.get("center") or {}
    street = " ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x)
    siret = re.sub(r"\D", "", tags.get("ref:FR:SIRET", ""))
    return {
        "source": "osm",
        "source_id": f"{el.get('type')}/{el.get('id')}",
        "name": name,
        "category": refine_category(cat, name),
        "address": street,
        "postal_code": tags.get("addr:postcode", ""),
        "city": tags.get("addr:city", ""),
        "country": country,
        "phone": tags.get("phone") or tags.get("contact:phone") or "",
        "website": website,
        "emails": emails,
        "siren": siret[:9] if len(siret) >= 9 else "",
        "naf": "",
        "lat": el.get("lat", center.get("lat")),
        "lon": el.get("lon", center.get("lon")),
    }


async def overpass(session: aiohttp.ClientSession, query: str, label: str) -> list[dict] | None:
    for attempt in range(6):
        endpoint = ENDPOINTS[attempt % len(ENDPOINTS)]
        try:
            async with session.post(endpoint, data={"data": query}, headers=_HEADERS,
                                    timeout=aiohttp.ClientTimeout(total=1000)) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    if "error" in (data.get("remark") or "").lower():
                        log(f"[osm] {label} : {data['remark'][:120]} (nouvel essai)")
                    else:
                        return data.get("elements", [])
                elif r.status in (403, 406) and len(ENDPOINTS) > 1:
                    # serveur qui refuse ce client : on l'écarte pour toute la suite de la collecte
                    if endpoint in ENDPOINTS:
                        ENDPOINTS.remove(endpoint)
                        log(f"[osm] {endpoint} refuse les requêtes (HTTP {r.status}) : serveur écarté")
                    continue
                else:
                    log(f"[osm] {label} : HTTP {r.status} sur {endpoint} (nouvel essai)")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log(f"[osm] {label} : {type(exc).__name__} {str(exc)[:100]} (nouvel essai)")
        await asyncio.sleep(min(15 * (attempt + 1), 90))
    log(f"[osm] {label} : abandon après plusieurs échecs")
    return None


async def collect_osm(db: DB, session: aiohttp.ClientSession, categories: list[str], *, country: str = "FR",
                      departements: list[str] | None = None, custom_area: str | None = None) -> int:
    index = selector_index(categories)
    if not index:
        return 0
    all_selectors = list(index)
    if custom_area:
        jobs = [(area_custom(custom_area), all_selectors, custom_area)]
    elif departements or country.upper() == "FR":
        # par département : requêtes petites et fiables (une requête France entière expire ou est tronquée)
        departements = departements or list(DEPARTEMENTS)
        jobs = [(area_departement(d), all_selectors, f"département {d}") for d in departements]
    else:  # pays entier : une requête par catégorie pour garder des réponses raisonnables
        jobs = [(area_country(country), [tuple(s) for s in CATEGORIES[c]["osm"]], f"{country} / {c}")
                for c in categories if CATEGORIES[c]["osm"]]

    total = 0
    sem = asyncio.Semaphore(2)  # les serveurs Overpass publics tolèrent ~2 requêtes simultanées

    async def run(area_stmt, selectors, label):
        nonlocal total
        async with sem:
            elements = await overpass(session, build_query(area_stmt, selectors), label)
        if elements is None:
            return
        items = [b for el in elements if (b := element_to_business(el, index, country))]
        added = db.add_businesses(items)
        total += added
        log(f"[osm] {label} : {len(elements)} lieux, {added} nouvelles entreprises")

    await asyncio.gather(*(run(*job) for job in jobs))
    return total
