"""Source SIRENE via l'API publique https://recherche-entreprises.api.gouv.fr (France, gratuite).

Donne toutes les entreprises actives par code NAF et département (nom, adresse, SIREN).
Pas d'e-mail ni de site web : ceux-ci sont trouvés ensuite par l'étape `guess` (domaines devinés
puis vérifiés) ou via la source `search`.
"""
from __future__ import annotations

import asyncio

import aiohttp

from ..config import CATEGORIES, refine_category
from ..db import DB
from ..utils import RateLimiter, log

API = "https://recherche-entreprises.api.gouv.fr/search"
PER_PAGE = 25
MAX_PAGES = 400  # l'API ne renvoie pas plus de 10 000 résultats par requête


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def result_to_businesses(r: dict, category: str, naf: str) -> list[dict]:
    legal = r.get("nom_complet") or r.get("nom_raison_sociale") or ""
    if not legal or "NON-DIFFUSIBLE" in legal.upper():
        return []
    etabs = [e for e in (r.get("matching_etablissements") or []) if e.get("etat_administratif", "A") == "A"]
    if not etabs:
        etabs = [r.get("siege") or {}]
    out = []
    for e in etabs:
        enseignes = e.get("liste_enseignes") or []
        enseigne = (enseignes[0] if enseignes else "") or e.get("nom_commercial") or ""
        name = enseigne or legal
        out.append({
            "source": "sirene",
            "source_id": e.get("siret") or r.get("siren"),
            "name": name,
            "alt_name": legal if enseigne else "",
            "category": refine_category(category, name),
            "address": e.get("adresse") or "",
            "postal_code": e.get("code_postal") or "",
            "city": e.get("libelle_commune") or "",
            "country": "FR",
            "phone": "",
            "website": "",
            "siren": r.get("siren") or "",
            "naf": e.get("activite_principale") or r.get("activite_principale") or naf,
            "lat": _float(e.get("latitude")),
            "lon": _float(e.get("longitude")),
        })
    return out


async def _get(session: aiohttp.ClientSession, limiter: RateLimiter, params: dict) -> dict | None:
    for attempt in range(6):
        await limiter.wait()
        try:
            async with session.get(API, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                if r.status == 429 or r.status >= 500:
                    retry = r.headers.get("Retry-After", "")
                    await asyncio.sleep(min(float(retry) if retry.isdigit() else 2 ** attempt, 60))
                    continue
                log(f"[sirene] HTTP {r.status} pour {params}")
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            await asyncio.sleep(2 ** attempt)
    log(f"[sirene] abandon pour {params}")
    return None


async def collect_sirene(db: DB, session: aiohttp.ClientSession, categories: list[str],
                         departements: list[str], *, rate: float = 6.0) -> int:
    combos: dict[tuple[str, str], str] = {}
    for cat in categories:
        for naf in CATEGORIES[cat]["naf"]:
            for dep in departements:
                combos.setdefault((naf, dep), cat)
    if not combos:
        return 0
    log(f"[sirene] {len(combos)} requêtes NAF x département (limite {rate}/s)")
    limiter = RateLimiter(rate)
    sem = asyncio.Semaphore(10)
    total = 0

    def save(data: dict, cat: str, naf: str) -> int:
        items = [b for r in data.get("results", []) for b in result_to_businesses(r, cat, naf)]
        return db.add_businesses(items)

    async def one(naf: str, dep: str, cat: str) -> None:
        nonlocal total
        async with sem:
            base = {"activite_principale": naf, "departement": dep, "etat_administratif": "A",
                    "per_page": PER_PAGE}
            first = await _get(session, limiter, {**base, "page": 1})
            if not first:
                return
            added = save(first, cat, naf)
            if (first.get("total_results") or 0) > PER_PAGE * MAX_PAGES:
                log(f"[sirene] {naf} / {dep} : {first['total_results']} résultats, seuls 10 000 accessibles")
            pages = min(int(first.get("total_pages") or 1), MAX_PAGES)
            for page in range(2, pages + 1):
                data = await _get(session, limiter, {**base, "page": page})
                if data:
                    added += save(data, cat, naf)
            total += added
            log(f"[sirene] NAF {naf} / dép. {dep} : {added} entreprises")

    await asyncio.gather(*(one(naf, dep, cat) for (naf, dep), cat in combos.items()))
    return total
