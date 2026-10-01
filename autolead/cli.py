"""Ligne de commande : python -m autolead <commande> [options]"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys

from .config import CATEGORIES, DEFAULT_CITIES, DEPARTEMENTS, PREFECTURES
from .crawler import run_crawl
from .db import DB
from .export import export_csv, export_phones_csv
from .guess import run_guess
from .net import DEFAULT_UA, make_session
from .sources import (
    PublicDataError,
    collect_dgccrf,
    collect_file,
    collect_gmaps,
    collect_osm,
    collect_search,
    collect_sirene,
    enrich_dinum,
)
from .utils import log
from .verify import run_verify


def _run(coro):
    try:
        import uvloop  # optionnel, plus rapide (Linux/macOS)

        if hasattr(uvloop, "run"):
            return uvloop.run(coro)
    except ImportError:
        pass
    return asyncio.run(coro)


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("un entier strictement positif est requis") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("la valeur doit être strictement positive")
    return number


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("un nombre strictement positif est requis") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("la valeur doit être finie et strictement positive")
    return number


SOURCE_NAMES = frozenset({"osm", "sirene", "gmaps", "search", "file", "dgccrf", "dinum"})


def source_names(value: str) -> str:
    if value == "auto":
        return value
    names = _split(value)
    unknown = set(names) - SOURCE_NAMES
    if not names or unknown:
        detail = f" : {', '.join(sorted(unknown))}" if unknown else ""
        raise argparse.ArgumentTypeError(
            f"sources inconnues ou liste vide{detail} ; choisir {', '.join(sorted(SOURCE_NAMES))} ou auto"
        )
    return value


def parse_categories(value: str | None) -> list[str]:
    if not value or value == "all":
        return list(CATEGORIES)
    cats = _split(value)
    unknown = [c for c in cats if c not in CATEGORIES]
    if unknown:
        sys.exit(f"Catégories inconnues : {', '.join(unknown)} (voir `python -m autolead categories`)")
    return cats


def parse_departements(value: str | None) -> list[str] | None:
    if not value:
        return None
    if value == "all":
        return list(DEPARTEMENTS)
    deps = [d.upper().zfill(2) for d in _split(value)]
    unknown = [d for d in deps if d not in DEPARTEMENTS]
    if unknown:
        sys.exit(f"Départements inconnus : {', '.join(unknown)}")
    return deps


def parse_dns_servers(value: str) -> list[str] | None:
    if value == "public":
        return None
    if value == "system":
        return []
    return _split(value)


def parse_cities(value: str | None) -> list[str]:
    if not value:
        return DEFAULT_CITIES
    if value == "all":
        return list(dict.fromkeys(DEFAULT_CITIES + PREFECTURES))
    return _split(value)


def resolve_sources(args) -> set[str]:
    brave_key = args.brave_key or os.environ.get("BRAVE_API_KEY")
    if args.sources == "auto":
        sources = {"osm"}
        if args.country.upper() == "FR":
            sources.add("sirene")
        if brave_key:
            sources.add("search")
        if args.google_key or os.environ.get("GOOGLE_MAPS_API_KEY"):
            sources.add("gmaps")
    else:
        sources = set(_split(args.sources))
    if args.file:
        sources.add("file")
    return sources


async def _gather_sources(coroutines: list) -> None:
    """Arrêter les autres collecteurs si l'un échoue, avant de fermer session/base."""
    tasks = [asyncio.create_task(coro) for coro in coroutines]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def collect(db: DB, args) -> None:
    sources = resolve_sources(args)
    cats = parse_categories(args.categories)
    deps = parse_departements(args.depts)
    if deps and args.country.upper() != "FR" and "osm" in sources and not args.osm_area:
        sys.exit("--depts ne s'applique qu'à la France ; utilisez --osm-area pour un autre pays.")
    log(f"[collect] sources : {', '.join(sorted(sources))} | catégories : {len(cats)}")

    async with make_session(concurrency=30, per_host=10, timeout=120, user_agent=args.user_agent) as session:
        tasks = []
        if "osm" in sources:
            tasks.append(collect_osm(db, session, cats, country=args.country, departements=deps,
                                     custom_area=args.osm_area))
        if "sirene" in sources:
            if args.country.upper() != "FR":
                log("[sirene] ignoré : la base SIRENE ne concerne que la France")
            else:
                tasks.append(collect_sirene(db, session, cats, deps or list(DEPARTEMENTS), rate=args.sirene_rate))
        if "search" in sources:
            key = args.brave_key or os.environ.get("BRAVE_API_KEY")
            if not key:
                log("[search] ignoré : définissez BRAVE_API_KEY ou --brave-key")
            else:
                cities = parse_cities(args.cities)
                tasks.append(collect_search(db, session, cats, cities, api_key=key, pages=args.search_pages,
                                            keywords=_split(args.keywords) or None,
                                            qps=args.search_qps, country=args.country))
        if "gmaps" in sources:
            key = args.google_key or os.environ.get("GOOGLE_MAPS_API_KEY")
            if not key:
                log("[gmaps] ignoré : définissez GOOGLE_MAPS_API_KEY ou --google-key")
            else:
                cities = parse_cities(args.cities)
                tasks.append(collect_gmaps(db, session, cats, cities, api_key=key, pages=args.gmaps_pages,
                                           qps=args.gmaps_qps, country=args.country))
        if "dgccrf" in sources:
            tasks.append(collect_dgccrf(db, session, departements=deps, categories=cats,
                                        limit=args.source_limit, path=args.dgccrf_file))
        await _gather_sources(tasks)
        if "file" in sources and args.file:
            collect_file(db, args.file, args.file_category)
        # La jointure doit voir les SIRET de TOUS les imports de cette commande.
        if "dinum" in sources:
            await enrich_dinum(db, session, path=args.dinum_file, limit=args.source_limit)


async def crawl(db: DB, args) -> None:
    if args.recrawl_no_email:
        log(f"[crawl] {db.reset_sites_without_email()} sites sans e-mail remis en file")
    if args.retry_failed:
        log(f"[crawl] {db.reset_failed_sites()} sites en échec remis en file")
    await run_crawl(db, concurrency=args.concurrency, max_pages=args.max_pages, timeout=args.timeout,
                    site_timeout=args.site_timeout, user_agent=args.user_agent, limit=args.limit,
                    follow_partners=not args.no_partners, deep=args.deep, deep_pages=args.deep_pages)


def export(db: DB, args) -> None:
    value = getattr(args, "export_categories", "all")
    cats = parse_categories(value) if value and value != "all" else None
    if getattr(args, "telephones", False):
        export_phones_csv(db, args.output, categories=cats, sep=args.sep)
        return
    export_csv(db, args.output, pro_only=args.pro_only, mx_only=args.mx_only, categories=cats,
               dedupe=not args.no_dedupe, sep=args.sep, attributed_only=args.attributed_only)


async def run_all(db: DB, args) -> None:
    await collect(db, args)
    if not args.no_guess and resolve_sources(args) & {"sirene", "gmaps"}:
        await run_guess(db, dns_concurrency=args.dns_concurrency)
    await crawl(db, args)
    if not args.no_verify:
        await run_verify(db, concurrency=args.dns_concurrency)
    export(db, args)
    print_stats(db)


def print_stats(db: DB) -> None:
    for key, value in db.stats().items():
        log(f"{key} : {value}")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default="leads.db", help="base SQLite (reprise automatique) [leads.db]")
    common.add_argument("--user-agent", default=DEFAULT_UA)

    p_collect = argparse.ArgumentParser(add_help=False)
    g = p_collect.add_argument_group("collecte")
    g.add_argument("--sources", default="auto", type=source_names,
                   help="osm,sirene,gmaps,search,file,dgccrf,dinum ; auto = osm + sirene + gmaps/search si clé")
    g.add_argument("--categories", default="all", help="liste séparée par des virgules, ou 'all'")
    g.add_argument("--country", default="FR", help="code pays ISO (OSM) [FR]")
    g.add_argument("--depts", help="départements, ex. 75,92,93 ou 'all' (défaut : France entière)")
    g.add_argument("--osm-area", help='zone Overpass libre, ex. \'area["name"="Lyon"]["admin_level"="8"]\'')
    g.add_argument("--sirene-rate", type=positive_float, default=6.0, help="requêtes/s API SIRENE (max 7) [6]")
    g.add_argument("--brave-key", help="clé API Brave Search (ou variable BRAVE_API_KEY)")
    g.add_argument("--google-key", help="clé API Google Places (ou variable GOOGLE_MAPS_API_KEY)")
    g.add_argument("--gmaps-pages", type=positive_int, default=3, help="pages de 20 résultats Google par requête (max 3) [3]")
    g.add_argument("--gmaps-qps", type=positive_float, default=5.0, help="requêtes/s Google Places [5]")
    g.add_argument("--cities", help="villes pour gmaps/search : liste séparée par des virgules, ou 'all'")
    g.add_argument("--keywords", help="mots-clés de recherche à la place de ceux des catégories")
    g.add_argument("--search-pages", type=positive_int, default=1, help="pages de 20 résultats par requête [1]")
    g.add_argument("--search-qps", type=positive_float, default=1.0, help="requêtes/s Brave (selon votre offre) [1]")
    g.add_argument("--file", help="fichier .txt (un site par ligne) ou .csv (colonne site/url/domaine)")
    g.add_argument("--file-category", default="autre")
    g.add_argument("--dgccrf-file", help="instantané local CSV/JSON DGCCRF, avec --sources dgccrf")
    g.add_argument("--dinum-file", help="instantané local CSV DINUM, avec --sources dinum")
    g.add_argument("--source-limit", type=positive_int,
                   help="examiner au plus N lignes par source DGCCRF/DINUM, avant les filtres (test)")

    p_crawl = argparse.ArgumentParser(add_help=False)
    g = p_crawl.add_argument_group("crawl")
    g.add_argument("--concurrency", type=positive_int, default=150, help="sites visités en parallèle [150]")
    g.add_argument("--max-pages", type=positive_int, default=10, help="budget de pages par site et par passage [10]")
    g.add_argument("--timeout", type=positive_float, default=15.0, help="timeout par page en s [15]")
    g.add_argument("--site-timeout", type=positive_float, default=60.0, help="temps max par site et par passage en s [60]")
    g.add_argument("--limit", type=positive_int, help="ne traiter que N sites (test)")
    g.add_argument("--retry-failed", action="store_true", help="re-tenter les sites injoignables")
    g.add_argument("--deep", action="store_true",
                   help="explorer les liens internes au-delà de contact/mentions ; automatique pour les réseaux")
    g.add_argument("--deep-pages", type=positive_int, default=60,
                   help="budget de pages par site et par passage en mode réseau, avec reprise [60]")
    g.add_argument("--no-partners", action="store_true",
                   help="ne pas suivre les liens vers d'autres sites auto (partenaires, réseau...)")
    g.add_argument("--recrawl-no-email", action="store_true",
                   help="revisiter les sites déjà crawlés où aucun e-mail n'a été trouvé")

    p_dns = argparse.ArgumentParser(add_help=False)
    p_dns.add_argument("--dns-concurrency", type=positive_int, default=300, help="requêtes DNS simultanées [300]")

    p_export = argparse.ArgumentParser(add_help=False)
    g = p_export.add_argument_group("export")
    g.add_argument("-o", "--output", default="leads_auto.csv")
    g.add_argument("--pro-only", action="store_true", help="exclure gmail/orange/free... (domaines pro uniquement)")
    g.add_argument("--mx-only", action="store_true", help="uniquement les domaines au MX valide")
    g.add_argument("--attributed-only", action="store_true",
                   help="uniquement les e-mails rattachés à un établissement par une preuve conservée")
    g.add_argument("--no-dedupe", action="store_true", help="garder les doublons (une ligne par entreprise)")
    g.add_argument("--sep", default=";", help="séparateur CSV [;]")
    g.add_argument("--telephones", action="store_true",
                   help="exporter les entreprises avec téléphone (même sans e-mail), une ligne par entreprise")

    parser = argparse.ArgumentParser(
        prog="autolead",
        description="Collecte d'e-mails professionnels du secteur automobile (garages, concessions, CT...).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", parents=[common, p_collect, p_crawl, p_dns, p_export],
                           help="tout enchaîner : collecte -> domaines devinés -> crawl -> MX -> export")
    p_run.add_argument("--no-guess", action="store_true", help="ne pas deviner les sites des fiches SIRENE")
    p_run.add_argument("--no-verify", action="store_true", help="ne pas vérifier les MX")
    sub.add_parser("collect", parents=[common, p_collect], help="récupérer les entreprises (OSM, SIRENE...)")
    p_guess = sub.add_parser("guess", parents=[common, p_dns], help="deviner le site des entreprises sans site")
    p_guess.add_argument("--limit", type=positive_int)
    p_guess.add_argument("--dns-servers", default="public",
                         help="'public' (Cloudflare/Google/Quad9, rapide), 'system' (DNS de la box) ou liste d'IP")
    sub.add_parser("crawl", parents=[common, p_crawl], help="visiter les sites et extraire les e-mails")
    p_verify = sub.add_parser("verify", parents=[common, p_dns], help="vérifier les MX des domaines e-mail")
    p_verify.add_argument("--recheck", action="store_true")
    p_export_cmd = sub.add_parser("export", parents=[common, p_export], help="exporter en CSV")
    p_export_cmd.add_argument("--categories", dest="export_categories", default="all",
                              help="n'exporter que ces catégories (ex. garage,carrosserie)")
    sub.add_parser("stats", parents=[common], help="statistiques de la base")
    sub.add_parser("repair-contacts", parents=[common],
                   help="sauvegarder et corriger les anciens rattachements, puis remettre les pages à vérifier en file")
    sub.add_parser("categories", help="lister les catégories ciblées")
    return parser


def validate_options(parser: argparse.ArgumentParser, args) -> None:
    if args.cmd in {"collect", "run"}:
        sources = resolve_sources(args)
        for name in ("dgccrf", "dinum"):
            if getattr(args, f"{name}_file") and name not in sources:
                parser.error(f"--{name}-file nécessite de choisir {name} dans --sources")
        if args.source_limit is not None and not sources & {"dgccrf", "dinum"}:
            parser.error("--source-limit s'applique uniquement aux sources dgccrf et dinum")
        if sources & {"dgccrf", "dinum"} and args.country.upper() != "FR":
            parser.error("les sources dgccrf et dinum concernent uniquement la France (--country FR)")
        if "file" in sources and not args.file:
            parser.error("--sources file nécessite --file")
        if args.gmaps_pages > 3:
            parser.error("--gmaps-pages doit être compris entre 1 et 3")
        if args.sirene_rate > 7:
            parser.error("--sirene-rate doit être inférieur ou égal à 7")
    if args.cmd in {"export", "run"}:
        if args.telephones and args.attributed_only:
            parser.error("--attributed-only s'applique à l'export e-mails, sans --telephones")
        if len(args.sep) != 1 or args.sep in "\r\n\0":
            parser.error("--sep doit être un seul caractère, hors saut de ligne et caractère nul")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_options(parser, args)
    if args.cmd == "categories":
        for key, c in CATEGORIES.items():
            osm = ", ".join(f"{k}={v}" for k, v in c["osm"]) or "-"
            print(f"{key:20} {c['label']}\n{'':20} NAF: {', '.join(c['naf']) or '-'} | OSM: {osm}")
        return

    db = DB(args.db)
    try:
        if args.cmd == "run":
            _run(run_all(db, args))
        elif args.cmd == "collect":
            _run(collect(db, args))
            print_stats(db)
        elif args.cmd == "guess":
            _run(run_guess(db, dns_concurrency=args.dns_concurrency, limit=args.limit,
                           dns_servers=parse_dns_servers(args.dns_servers)))
        elif args.cmd == "crawl":
            _run(crawl(db, args))
        elif args.cmd == "verify":
            _run(run_verify(db, concurrency=args.dns_concurrency, recheck=args.recheck))
        elif args.cmd == "export":
            export(db, args)
        elif args.cmd == "stats":
            print_stats(db)
        elif args.cmd == "repair-contacts":
            for key, value in db.repair_contacts().items():
                log(f"[repair] {key} : {value}")
            log("[repair] Les pages remises en file seront traitées au prochain crawl.")
            print_stats(db)
    except PublicDataError as exc:
        log(f"[collect] échec de la source publique : {exc}")
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        log("\nInterrompu : la progression est enregistrée, relancez la même commande pour reprendre.")
        raise SystemExit(130)
    finally:
        db.close()
