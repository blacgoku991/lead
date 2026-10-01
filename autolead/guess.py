"""Trouve le site des entreprises qui n'en ont pas (ex. SIRENE) en devinant le domaine.

"GARAGE DUPONT" -> garagedupont.fr, garage-dupont.fr, garagedupont.com, garage-dupont.com
Seuls les domaines qui existent (DNS) sont crawlés, et un site n'est retenu que si ses pages
contiennent le code postal ou le SIREN de l'entreprise (évite les homonymes).
"""
from __future__ import annotations

import asyncio
import re
import unicodedata
from collections import defaultdict

import dns.asyncresolver
import dns.exception

from .db import DB
from .utils import Progress, log

LEGAL_FORMS = {
    "sarl", "sas", "sasu", "eurl", "sa", "sci", "snc", "selarl", "scop", "ei", "eirl", "ets",
    "etablissements", "etablissement", "ste", "societe", "sarlu", "scs", "gie", "earl", "sca",
}
# Un nom composé uniquement de ces mots est trop générique pour deviner un domaine
GENERIC = {
    "garage", "garages", "auto", "autos", "automobile", "automobiles", "car", "cars", "carrosserie",
    "mecanique", "service", "services", "centre", "center", "station", "du", "de", "des", "la", "le",
    "les", "l", "d", "et", "moto", "motos", "pneu", "pneus", "controle", "technique", "location",
    "ecole", "conduite", "depannage", "lavage", "france", "sud", "nord", "est", "ouest", "st",
    "saint", "sainte", "a", "au", "aux", "en", "the", "occasion", "occasions", "vente", "pieces",
}


def _words(name: str) -> list[str]:
    name = re.sub(r"\(.*?\)", " ", name or "")  # "GARAGE DUPONT (DUPONT JEAN)" -> "GARAGE DUPONT"
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " et ")
    return [w for w in re.findall(r"[a-z0-9]+", s) if w not in LEGAL_FORMS]


def candidate_domains(name: str, tlds=("fr", "com")) -> list[str]:
    words = _words(name)
    if not words or len(words) > 5 or all(w in GENERIC for w in words):
        return []
    bases = {"".join(words), "-".join(words)}
    return [f"{b}.{t}" for b in sorted(bases) if 4 <= len(b) <= 63 for t in tlds]


def business_tokens(postal_code: str | None, siren: str | None) -> list[str]:
    tokens = []
    if postal_code and re.fullmatch(r"\d{5}", postal_code.strip()):
        tokens.append(postal_code.strip())
    if siren and re.fullmatch(r"\d{9}", siren.strip()):
        tokens.append(siren.strip())
    return tokens


async def _exists(resolver, domain: str) -> bool:
    try:
        await resolver.resolve(domain, "A")
        return True
    except dns.exception.DNSException:
        return False


async def run_guess(db: DB, *, dns_concurrency: int = 300, limit: int | None = None) -> int:
    rows = db.businesses_for_guess(limit)
    known = db.known_site_keys()
    candidates: dict[str, list[tuple[int, list[str]]]] = defaultdict(list)
    for r in rows:
        tokens = business_tokens(r["postal_code"], r["siren"])
        if not tokens:
            continue
        for d in set(candidate_domains(r["name"])) | set(candidate_domains(r["alt_name"] or "")):
            if d not in known:
                candidates[d].append((r["id"], tokens))
    if not candidates:
        log("[guess] aucun domaine à tester.")
        return 0
    log(f"[guess] {len(rows)} entreprises sans site -> {len(candidates)} domaines à tester (DNS)")

    try:
        resolver = dns.asyncresolver.Resolver()
    except dns.exception.DNSException as exc:
        log(f"[guess] résolveur DNS indisponible : {exc!r}")
        return 0
    resolver.timeout, resolver.lifetime = 3.0, 6.0
    sem = asyncio.Semaphore(dns_concurrency)
    progress = Progress("guess DNS", total=len(candidates))
    alive: dict[str, list] = {}

    async def check(domain: str) -> None:
        async with sem:
            if await _exists(resolver, domain):
                alive[domain] = candidates[domain]
                progress.add("existants", 1)
        progress.tick()

    ticker = asyncio.create_task(progress.run())
    try:
        await asyncio.gather(*(check(d) for d in candidates))
    finally:
        ticker.cancel()
    db.add_guesses(alive)
    log(f"[guess] {len(alive)} domaines existants ajoutés au crawl (vérifiés pendant le crawl)")
    return len(alive)
