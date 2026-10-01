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
import dns.resolver

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


def candidate_domains(name: str, city: str = "", tlds=("fr", "com")) -> list[str]:
    """Variantes de domaine : nom complet, nom + ville, nom sans mot générique (+ auto / garage)."""
    words = _words(name)
    if not words or len(words) > 5 or all(w in GENERIC for w in words):
        return []
    bases: set[str] = set()

    def add(ws: list[str]) -> None:
        bases.update({"".join(ws), "-".join(ws)})

    add(words)
    main = set(bases)
    city_words = _words(city)[:3]
    if city_words and city_words[-len(city_words):] != words[-len(city_words):]:
        add(words + city_words)                      # garage-dupont-lyon
    core = [w for w in words if w not in GENERIC]
    if core and core != words and len("".join(core)) >= 5:
        add(core)                                     # dupont
        add(core + ["auto"])                          # dupont-auto
        if words[0] != "garage":
            add(["garage"] + core)                    # garage-dupont
    out = [f"{b}.{t}" for b in sorted(bases) if 4 <= len(b) <= 63 for t in tlds]
    out += [f"{b}.net" for b in sorted(main) if 4 <= len(b) <= 63]
    return out


def business_tokens(postal_code: str | None, siren: str | None) -> list[str]:
    tokens = []
    if postal_code and re.fullmatch(r"\d{5}", postal_code.strip()):
        tokens.append(postal_code.strip())
    if siren and re.fullmatch(r"\d{9}", siren.strip()):
        tokens.append(siren.strip())
    return tokens


# Résolveurs publics rapides : le DNS de la box limite souvent à quelques dizaines de requêtes/s
PUBLIC_DNS = ["1.1.1.1", "8.8.8.8", "9.9.9.9", "1.0.0.1", "8.8.4.4", "149.112.112.112"]


async def _exists(resolver, domain: str) -> bool | None:
    """True = existe, False = n'existe pas, None = pas de réponse (sera retesté au prochain lancement)."""
    try:
        await resolver.resolve(domain, "A")
        return True
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return False
    except dns.exception.DNSException:
        return None


def make_resolver(servers: list[str] | None):
    if servers == []:  # résolveur du système
        resolver = dns.asyncresolver.Resolver()
    else:
        resolver = dns.asyncresolver.Resolver(configure=False)
        resolver.nameservers = servers or PUBLIC_DNS
        resolver.rotate = True
    resolver.timeout, resolver.lifetime = 2.0, 4.0
    return resolver


async def run_guess(db: DB, *, dns_concurrency: int = 300, limit: int | None = None,
                    dns_servers: list[str] | None = None) -> int:
    rows = db.businesses_for_guess(limit)
    skip = db.known_site_keys() | db.dns_checked()
    candidates: dict[str, list[tuple[int, list[str]]]] = defaultdict(list)
    for r in rows:
        tokens = business_tokens(r["postal_code"], r["siren"])
        if not tokens:
            continue
        city = r["city"] or ""
        for d in set(candidate_domains(r["name"], city)) | set(candidate_domains(r["alt_name"] or "", city)):
            if d not in skip:
                candidates[d].append((r["id"], tokens))
    if not candidates:
        log("[guess] aucun nouveau domaine à tester.")
        return 0
    log(f"[guess] {len(rows)} entreprises sans site -> {len(candidates)} domaines à tester (DNS)")

    try:
        resolver = make_resolver(dns_servers)
    except dns.exception.DNSException as exc:
        log(f"[guess] résolveur DNS indisponible : {exc!r}")
        return 0
    queue: asyncio.Queue = asyncio.Queue()
    for d in candidates:
        queue.put_nowait(d)
    progress = Progress("guess DNS", total=len(candidates))
    alive: dict[str, list] = {}
    checked: list[tuple[str, int]] = []
    found = 0

    def flush() -> None:
        # sauvegarde régulière : un Ctrl+C ne fait perdre que les dernières secondes
        db.add_guesses(alive)
        db.save_dns(checked)
        db.commit()
        alive.clear()
        checked.clear()

    async def worker() -> None:
        nonlocal found
        while True:
            try:
                domain = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            status = await _exists(resolver, domain)
            if status is not None:
                checked.append((domain, int(status)))
            if status:
                alive[domain] = candidates[domain]
                found += 1
                progress.add("existants", 1)
            elif status is None:
                progress.add("sans réponse", 1)
            progress.tick()
            if len(checked) >= 2000:
                flush()

    ticker = asyncio.create_task(progress.run())
    try:
        await asyncio.gather(*(worker() for _ in range(dns_concurrency)))
    finally:
        ticker.cancel()
        flush()
    log(f"[guess] {found} domaines existants ajoutés au crawl (vérifiés pendant le crawl)")
    return found
