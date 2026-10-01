"""Crawler asynchrone : visite la page d'accueil + contact / mentions légales de chaque site.

Des centaines de sites sont traités en parallèle ; chaque site est limité à quelques pages
et quelques requêtes simultanées pour rester rapide et ne pas surcharger les petits serveurs.
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import aiohttp

from .db import DB
from .extract import discover_external_sites, discover_links, extract_emails, extract_phones, internal_links
from .frontier import Frontier, canonical_url, is_network_key
from .identity import extract_entities, match_identity
from .net import DEFAULT_UA, make_session
from .utils import Progress, host_of, log, registrable

FALLBACK_PATHS = (
    "/contact", "/nous-contacter", "/mentions-legales", "/contactez-nous", "/contact.html", "/contact.php",
)
_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([a-zA-Z0-9_-]+)""", re.I)
_NETWORK_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, ValueError, UnicodeError, OSError)


@dataclass
class Page:
    url: str
    text: str
    truncated: bool = False


@dataclass
class CrawlResult:
    status: str = "pending"
    final_url: str = ""
    pages: int = 0
    emails: dict = field(default_factory=dict)  # e-mail -> URL de la page où il a été trouvé
    phones: set = field(default_factory=set)
    texts: list = field(default_factory=list)  # contenu des pages (vérification des domaines devinés)
    partners: set = field(default_factory=set)  # sites auto externes cités (partenaires, réseau...)
    entities: list[dict] = field(default_factory=list)  # chaque fiche garde son identité et sa source
    is_network: bool = False
    completed_urls: set[str] = field(default_factory=set)
    failed_urls: dict[str, str] = field(default_factory=dict)
    blocked_urls: set[str] = field(default_factory=set)
    frontier_counts: dict[str, int] = field(default_factory=dict)
    _entity_keys: set[tuple] = field(default_factory=set, repr=False)
    _reported_email_site: bool = field(default=False, repr=False)


def _decode(body: bytes, charset: str | None) -> str:
    if not charset:
        m = _META_CHARSET.search(body[:4096])
        charset = m.group(1).decode("ascii", "ignore") if m else None
    if charset:
        try:
            return body.decode(charset, "replace")
        except LookupError:
            pass
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", "replace")


def _norm(url: str) -> str:
    return canonical_url(url) or url


def _same_site(url: str, base: str) -> bool:
    """Autorise les sous-domaines d'un réseau sans perdre les ports des tests locaux."""
    try:
        p, b = urlsplit(url), urlsplit(base)
        if p.scheme not in ("http", "https"):
            return False
        if registrable(p.hostname or "") != registrable(b.hostname or ""):
            return False
        if re.fullmatch(r"[\d.:]+", b.hostname or "") and p.port != b.port:
            return False
        return True
    except ValueError:
        return False


class _Pagination(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ("a", "link"):
            return
        values = dict(attrs)
        if "next" in (values.get("rel") or "").lower().split() and values.get("href"):
            self.urls.append(values["href"])


_NETWORK_PAGE_HINT = re.compile(
    r"garage|carross|atelier|agence|centre|magasin|concession|dealer|workshop|\bdetails\b|\bcust-", re.I)


def _enqueue_pages(frontier: Frontier, urls, *, source: str, priority: int = 35) -> None:
    branches, other = [], []
    for url in urls:
        (branches if _NETWORK_PAGE_HINT.search(urlsplit(url).path) else other).append(url)
    frontier.enqueue(branches, priority=priority, discovered_from=source)
    frontier.enqueue(other, priority=60, discovered_from=source)


class Crawler:
    def __init__(self, session: aiohttp.ClientSession, *, max_pages: int = 10, max_bytes: int = 1_500_000,
                 user_agent: str = DEFAULT_UA, per_site_parallel: int = 3, respect_robots: bool = True,
                 follow_partners: bool = True, host_limits: dict | None = None):
        self.session = session
        self.max_pages = max_pages
        self.max_bytes = max_bytes
        self.user_agent = user_agent
        self.robots_agent = "AutoLeadBot" if re.search(r"\bAutoLeadBot\b", user_agent, re.I) else user_agent
        self.per_site_parallel = per_site_parallel
        self.respect_robots = respect_robots
        self.follow_partners = follow_partners
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        limits = host_limits if host_limits is not None else {}
        self._host_semaphores = limits.setdefault("semaphores", {})
        self._host_locks = limits.setdefault("locks", {})
        self._next_request = limits.setdefault("next_request", {})

    async def crawl(self, url: str, res: CrawlResult, keep_text: bool = False,
                    deep_pages: int = 0, *, frontier: Frontier | None = None,
                    seed_urls=(), checkpoint=None) -> CrawlResult:
        """deep_pages est un budget par passage, avec reprise persistante si frontier est fourni."""
        if deep_pages:
            temporary = None
            if frontier is None:
                temporary = sqlite3.connect(":memory:")
                frontier = Frontier(temporary, url)
            try:
                return await self._crawl_network(url, res, frontier, deep_pages, keep_text,
                                                 seed_urls=seed_urls, checkpoint=checkpoint)
            finally:
                if temporary is not None:
                    temporary.close()
        robots = self._robots
        # Une nouvelle source peut fournir une fiche locale alors que la racine
        # enregistrée autrefois ne fonctionne plus. Les URL complètes récentes
        # passent avant les variantes www/http et les chemins contact devinés.
        start_urls = []
        raw_urls = [*reversed(list(seed_urls)), url]
        raw_urls.sort(key=lambda item: urlsplit(item).path in ("", "/"))
        for raw in raw_urls:
            candidate = canonical_url(raw if "://" in raw else "https://" + raw)
            if candidate and _same_site(candidate, url) and candidate not in start_urls:
                start_urls.append(candidate)
        start_urls = start_urls[:max(1, self.max_pages)]
        candidates = list(dict.fromkeys([*start_urls, *self._home_candidates(url)]))
        attempted: set[str] = set()
        home = None
        for candidate in candidates:
            attempted.add(_norm(candidate))
            home = await self._fetch(candidate, robots)
            if home:
                break
        if home is None:
            res.status = "unreachable"
            return res
        res.final_url = home.url
        self._consume(home, res, keep_text)

        seen = attempted | {_norm(home.url)}
        remaining_seeds = [u for u in start_urls if _norm(u) not in seen]
        seed_pages = await self._fetch_batch(remaining_seeds[:self.max_pages - res.pages],
                                            res, seen, robots, keep_text)
        links = []
        for page in [home, *seed_pages]:
            links += [u for u in discover_links(page.text, page.url)
                      if _norm(u) not in seen and u not in links]
        contact_pages = await self._fetch_batch(links[:self.max_pages - res.pages], res, seen, robots, keep_text)
        pages = [*seed_pages, *contact_pages]

        # 2e niveau : liens contact / mentions trouvés sur les pages déjà visitées
        if res.pages < self.max_pages:
            more: list[str] = []
            for p in pages:
                more += [u for u in discover_links(p.text, p.url) if _norm(u) not in seen and u not in more]
            await self._fetch_batch(more[: self.max_pages - res.pages], res, seen, robots, keep_text)

        if not res.emails and res.pages < self.max_pages:
            extra = [urljoin(home.url, p) for p in FALLBACK_PATHS]
            extra = [u for u in extra if _norm(u) not in seen]
            await self._fetch_batch(extra[: self.max_pages - res.pages], res, seen, robots, keep_text)

        if not res.emails and res.pages < self.max_pages:
            extra = [u for u in await self._sitemap_contacts(home.url, robots) if _norm(u) not in seen]
            await self._fetch_batch(extra[: self.max_pages - res.pages], res, seen, robots, keep_text)

        res.status = "ok"
        return res

    async def _crawl_network(self, url, res, frontier, budget, keep_text, *, seed_urls=(), checkpoint=None):
        """Explore une file durable ; les pages traitées ne sont pas téléchargées au passage suivant."""
        url = canonical_url(url if "://" in url else "https://" + url) or url
        origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
        frontier.enqueue([url, *[u for u in seed_urls if _same_site(u, url)]], priority=0)
        listing = [origin + "/"]
        if is_network_key(url):
            host = host_of(url)
            if host == "top-garage.fr" or host.endswith(".top-garage.fr"):
                listing.append("https://garage.top-garage.fr/fr/france-FR/all")
            elif host == "axial.org" or host.endswith(".axial.org"):
                listing.append(origin + "/nos-carrossiers-axial")
        frontier.enqueue(listing, priority=20)
        # Charge robots avant de lire les Sitemap: déclarés (y compris les index).
        await self._allowed(url, self._robots)
        rp = self._robots.get(origin)
        declared = rp.site_maps() if rp is not None else []
        frontier.enqueue(declared or (), kind="sitemap", priority=5, discovered_from=origin + "/robots.txt")
        frontier.enqueue([origin + "/sitemap.xml", origin + "/sitemap_index.xml"],
                         kind="sitemap", priority=10)

        attempted: set[str] = set()
        page_attempts = sitemap_attempts = 0
        sitemap_budget = 8
        res.status = "partial"

        def acknowledge():
            if checkpoint is not None:
                checkpoint()
            else:
                # Le résultat direct est rendu à l'appelant ; la production fournit
                # un checkpoint qui sauvegarde les contacts AVANT ces validations.
                _ack_frontier(frontier, res)
                frontier.conn.commit()

        while page_attempts < budget or sitemap_attempts < sitemap_budget:
            candidates = []
            if page_attempts < budget:
                candidates += frontier.pending(min(self.per_site_parallel, budget - page_attempts),
                                               kind="page", exclude=attempted)
            if sitemap_attempts < sitemap_budget:
                candidates += frontier.pending(min(self.per_site_parallel, sitemap_budget - sitemap_attempts),
                                               kind="sitemap", exclude=attempted)
            todo = sorted(candidates, key=lambda item: (item["priority"], item["rowid"]))[:self.per_site_parallel]
            if not todo:
                break
            for item in todo:
                attempted.add(item["url"])
                if item["kind"] == "page":
                    page_attempts += 1
                else:
                    sitemap_attempts += 1

            async def visit(item):
                target = item["url"]
                outcome = {}
                page = await self._fetch(target, self._robots, outcome=outcome)
                if page is None:
                    reason = outcome.get("reason", "fetch_failed")
                    # Les emplacements conventionnels de sitemap sont facultatifs.
                    if (item["kind"] == "sitemap" and item["priority"] == 10
                            and reason in ("http_404", "http_410")):
                        res.completed_urls.add(target)
                    elif reason == "robots_disallow":
                        res.blocked_urls.add(target)
                    else:
                        res.failed_urls[target] = reason
                    return
                if page.truncated:
                    frontier.mark_limited()
                if item["kind"] == "sitemap":
                    try:
                        tree = ElementTree.fromstring(page.text)
                        root_kind = tree.tag.rsplit("}", 1)[-1].lower()
                    except ElementTree.ParseError:
                        root_kind = ""
                    if root_kind not in ("urlset", "sitemapindex"):
                        res.failed_urls[target] = "invalid_sitemap"
                        return
                    locations = [node.text.strip() for node in tree.iter()
                                 if node.tag.rsplit("}", 1)[-1].lower() == "loc" and node.text]
                    if root_kind == "sitemapindex":
                        # Les sous-sitemaps ne sont suivis que sur le domaine du sitemap
                        # public, ou celui du réseau. Aucun endpoint privé n'est interrogé.
                        locations = [u for u in locations if _same_site(u, page.url) or _same_site(u, url)]
                        frontier.enqueue(locations, kind="sitemap", priority=15, discovered_from=page.url)
                    else:
                        locations = [u for u in locations if _same_site(u, url)]
                        _enqueue_pages(frontier, locations, priority=30, source=page.url)
                else:
                    if not res.final_url:
                        res.final_url = page.url
                    self._consume(page, res, keep_text)
                    links = internal_links(page.text, page.url, limit=5000)
                    if len(links) >= 5000 or len(re.findall(r"<a\b", page.text, re.I)) > 5000:
                        frontier.mark_limited()
                    parser = _Pagination()
                    try:
                        parser.feed(page.text)
                    except (ValueError, AssertionError):
                        pass
                    pages = [u for u in links if _same_site(u, url)]
                    _enqueue_pages(frontier, pages, source=page.url)
                    contacts = [u for u in discover_links(page.text, page.url, limit=100) if _same_site(u, url)]
                    frontier.enqueue(contacts, priority=25, discovered_from=page.url)
                    # Les fiches voisines peuvent être présentes uniquement dans le
                    # JSON-LD. Leur URL devient une page à visiter, même sans e-mail.
                    entity_urls = [e.get("website") for e in res.entities if e.get("website")]
                    frontier.enqueue([u for u in entity_urls if _same_site(u, url)],
                                     priority=25, discovered_from=page.url)
                    next_pages = [urljoin(page.url, u) for u in parser.urls]
                    frontier.enqueue([u for u in next_pages if _same_site(u, url)],
                                     priority=20, discovered_from=page.url)
                res.completed_urls.add(target)
                if _same_site(page.url, url):
                    frontier.enqueue([page.url], kind=item["kind"], priority=item["priority"])
                    res.completed_urls.add(page.url)

            # Chaque tâche ne marque sa page terminée qu'une fois son extraction
            # ajoutée au résultat. Même une annulation en cours de lot conserve cela.
            await asyncio.gather(*(visit(item) for item in todo))
            acknowledge()

        res.frontier_counts = frontier.counts()
        counts = res.frontier_counts
        res.status = "partial" if any(counts[k] for k in ("pending", "failed", "blocked", "discovery_limited")) else "ok"
        return res

    def _home_candidates(self, url: str) -> list[str]:
        p = urlsplit(url if "://" in url else "https://" + url)
        other = "http" if p.scheme == "https" else "https"
        path = (p.path or "/") + (f"?{p.query}" if p.query else "")
        hosts = [p.netloc]
        if not p.netloc.startswith("www.") and not re.match(r"^[\d.:]+$", p.netloc):
            hosts.append("www." + p.netloc)
        out = [f"{p.scheme}://{h}{path}" for h in hosts] + [f"{other}://{p.netloc}{path}"]
        if path != "/":
            out.append(f"{p.scheme}://{p.netloc}/")
        return out

    async def _fetch_batch(self, urls, res, seen, robots, keep_text) -> list[Page]:
        if not urls:
            return []
        for u in urls:
            seen.add(_norm(u))
        sem = asyncio.Semaphore(self.per_site_parallel)
        pages: list[Page] = []

        async def one(u: str) -> None:
            async with sem:
                page = await self._fetch(u, robots)
            if page:
                self._consume(page, res, keep_text)
                pages.append(page)

        await asyncio.gather(*(one(u) for u in urls))
        return pages

    def _consume(self, page: Page, res: CrawlResult, keep_text: bool) -> None:
        res.pages += 1
        for email in extract_emails(page.text):
            res.emails.setdefault(email, page.url)
        res.phones |= extract_phones(page.text)
        for entity in extract_entities(page.text, page.url):
            emails = entity.get("emails") or []
            entity_key = (entity.get("source_type"), entity.get("source_id"), entity.get("source_url"),
                          entity.get("name"), entity.get("siret"), tuple(sorted(emails)))
            if entity_key not in res._entity_keys:
                res._entity_keys.add(entity_key)
                res.entities.append(entity)
        if keep_text:
            res.texts.append(page.text[:400_000])
        if self.follow_partners:
            res.partners |= discover_external_sites(page.text, page.url)

    async def _sitemap_contacts(self, home_url: str, robots: dict) -> list[str]:
        """URLs de type contact / mentions / à propos listées dans le sitemap.xml."""
        origin = f"{urlsplit(home_url).scheme}://{urlsplit(home_url).netloc}"
        hint = re.compile(r"contact|mentions|legal|coordonn|a-propos|apropos|about|infos", re.I)
        urls: list[str] = []
        for sm in (origin + "/sitemap.xml", origin + "/sitemap_index.xml"):
            page = await self._fetch(sm, robots)
            if not page:
                continue
            locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", page.text, re.I)
            # sitemap index : on ouvre un sous-sitemap qui parle de contact/pages
            for sub in [u for u in locs if u.endswith(".xml") and hint.search(u)][:2]:
                subp = await self._fetch(sub, robots)
                if subp:
                    locs += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", subp.text, re.I)
            urls += [u for u in locs if not u.endswith(".xml") and hint.search(u)]
            if urls:
                break
        return urls[:6]

    async def _fetch(self, url: str, robots: dict, *, outcome: dict | None = None) -> Page | None:
        outcome = outcome if outcome is not None else {}
        for _ in range(7):
            if not await self._allowed(url, robots):
                p = urlsplit(url)
                rp = robots.get(f"{p.scheme}://{p.netloc}")
                outcome["reason"] = ("robots_unavailable" if getattr(rp, "_unavailable", False)
                                     else "robots_disallow")
                return None
            try:
                host = urlsplit(url).netloc.lower()
                sem = self._host_semaphores.setdefault(host, asyncio.Semaphore(self.per_site_parallel))
                async with sem:
                    await self._wait_host(url, robots)
                    # Chaque destination de redirection est soumise à ses propres robots.
                    async with self.session.get(url, allow_redirects=False) as r:
                        if r.status in (301, 302, 303, 307, 308):
                            target = canonical_url(urljoin(str(r.url), r.headers.get("Location", "")))
                            if not target or target == url:
                                outcome["reason"] = "invalid_redirect"
                                return None
                            url = target
                            continue
                        if r.status >= 400:
                            outcome["reason"] = f"http_{r.status}"
                            return None
                        ctype = r.headers.get("Content-Type", "").lower()
                        path = urlsplit(url).path.lower()
                        vcard = path.endswith(".vcf") or "/vcard" in path
                        if ctype and not vcard and not any(t in ctype for t in (
                                "html", "text/plain", "xml", "vcard", "directory")):
                            outcome["reason"] = "unsupported_content_type"
                            return None
                        chunks, size = [], 0
                        async for chunk in r.content.iter_chunked(65536):
                            chunk = chunk[:self.max_bytes + 1 - size]
                            chunks.append(chunk)
                            size += len(chunk)
                            if size > self.max_bytes:
                                break
                        body = b"".join(chunks)
                        return Page(str(r.url), _decode(body[:self.max_bytes], r.charset), size > self.max_bytes)
            except _NETWORK_ERRORS as exc:
                outcome["reason"] = "timeout" if isinstance(exc, asyncio.TimeoutError) else "network_error"
                return None
        outcome["reason"] = "too_many_redirects"
        return None

    async def _wait_host(self, url: str, robots: dict) -> None:
        p = urlsplit(url)
        host = p.netloc.lower()
        rp = robots.get(f"{p.scheme}://{p.netloc}")
        delay = rp.crawl_delay(self.robots_agent) if rp else 0
        rate = rp.request_rate(self.robots_agent) if rp else None
        delay = max(float(delay or 0), rate.seconds / rate.requests if rate and rate.requests else 0)
        if not delay:
            return
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            remaining = self._next_request.get(host, 0) - time.monotonic()
            while remaining > 0:
                await asyncio.sleep(min(remaining, 30))
                remaining = self._next_request.get(host, 0) - time.monotonic()
            self._next_request[host] = time.monotonic() + delay

    async def _allowed(self, url: str, robots: dict) -> bool:
        if not self.respect_robots:
            return True
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in robots:
            lock = self._robots_locks.setdefault(origin, asyncio.Lock())
            async with lock:
                if origin not in robots:
                    robots[origin] = await self._load_robots(origin)
        rp = robots[origin]
        return rp is None or rp.can_fetch(self.robots_agent, url)

    async def _load_robots(self, origin: str) -> RobotFileParser | None:
        try:
            host = urlsplit(origin).netloc.lower()
            sem = self._host_semaphores.setdefault(host, asyncio.Semaphore(self.per_site_parallel))
            async with sem:
                async with self.session.get(origin + "/robots.txt", allow_redirects=True, max_redirects=6,
                                            timeout=aiohttp.ClientTimeout(total=8)) as r:
                    if r.status in (404, 410):
                        return None
                    if r.status >= 400:
                        rp = RobotFileParser()
                        rp.disallow_all = True
                        rp._unavailable = r.status not in (401, 403)
                        return rp
                    body = await r.content.read(300_000)
        except _NETWORK_ERRORS:
            # Un robots inaccessible temporairement est réessayé lors d'une future
            # invocation ; il n'est pas interprété comme une autorisation de crawl.
            rp = RobotFileParser()
            rp.disallow_all = True
            rp._unavailable = True
            return rp
        rp = RobotFileParser()
        rp.parse(body.decode("utf-8", "replace").splitlines())
        return rp


def verify_tokens(text: str, tokens: list[str]) -> bool:
    """Compatibilité historique seulement : ne sert plus à confirmer une entreprise."""
    digits = re.sub(r"(?<=\d)[\s. -](?=\d)", "", text)
    for tok in tokens:
        src = digits if len(tok) == 9 else text
        if re.search(rf"(?<!\d){re.escape(tok)}(?!\d)", src):
            return True
    return False


async def run_crawl(db: DB, *, concurrency: int = 150, max_pages: int = 10, timeout: float = 15.0,
                    site_timeout: float = 60.0, user_agent: str = DEFAULT_UA, limit: int | None = None,
                    trust_env: bool = True, follow_partners: bool = True, rounds: int = 3,
                    deep: bool = False, deep_pages: int = 60) -> None:
    # getaddrinfo tourne dans un pool de threads : on l'agrandit pour ne pas brider le DNS
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=min(256, concurrency + 16)))
    # Un budget s'applique à une invocation : les tours partenaires ne doivent pas
    # redonner immédiatement 60 pages à chaque réseau encore partiellement traité.
    visited: set[str] = set()
    for n in range(1, (rounds if follow_partners and not limit else 1) + 1):
        sites = [site for site in db.pending_sites(limit) if site["site_key"] not in visited]
        if not sites:
            if n == 1:
                log("[crawl] aucun site en attente.")
            return
        label = "" if n == 1 else f" (tour {n} : sites partenaires découverts)"
        log(f"[crawl] {len(sites)} sites à visiter{label}, {concurrency} en parallèle")
        networks = db.network_sites()
        if networks and n == 1:
            log(f"[crawl] {len(networks)} réseaux connus ou domaines partagés (≥2 établissements) : "
                f"budget {deep_pages} pages par passage ; reprise des pages restantes")
        visited.update(site["site_key"] for site in sites)
        await _crawl_round(db, sites, concurrency=concurrency, max_pages=max_pages, timeout=timeout,
                           site_timeout=site_timeout, user_agent=user_agent, trust_env=trust_env,
                           follow_partners=follow_partners, deep=deep, deep_pages=deep_pages,
                           networks=networks)


async def _crawl_round(db: DB, sites, *, concurrency, max_pages, timeout, site_timeout, user_agent,
                       trust_env, follow_partners, deep=False, deep_pages=60, networks=frozenset()) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    for s in sites:
        queue.put_nowait(s)
    progress = Progress("crawl", total=len(sites))
    pending_commit = 0

    async with make_session(concurrency=concurrency * 3, per_host=4, timeout=timeout,
                            user_agent=user_agent, trust_env=trust_env) as session:
        host_limits: dict = {}
        async def worker() -> None:
            nonlocal pending_commit
            while True:
                try:
                    site = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                network = site["site_key"] in networks or is_network_key(site["site_key"])
                res = CrawlResult(is_network=network)
                # Cache robots borné aux sites actifs ; connexions, concurrence et
                # espacement des requêtes partagés si deux réseaux croisent un hôte.
                crawler = Crawler(session, max_pages=max_pages, user_agent=user_agent,
                                  follow_partners=follow_partners, host_limits=host_limits)
                go_deep = deep or network
                frontier = Frontier(db.conn, site["site_key"]) if go_deep else None
                seeds = [r[0] for r in db.conn.execute(
                    "SELECT website FROM businesses WHERE site_key=? AND COALESCE(website,'')<>''",
                    (site["site_key"],))]
                if db.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='business_pages'").fetchone():
                    seeds += [r[0] for r in db.conn.execute(
                        "SELECT url FROM business_pages WHERE site_key=? ORDER BY rowid", (site["site_key"],))]
                # Un domaine DINUM peut déjà appartenir à un site connu. Ses autres
                # propriétaires candidats doivent aussi être vérifiés.
                keep_text = bool(site["guessed"] or db.guess_owners(site["site_key"]))
                stored_partners: set[str] = set()

                def checkpoint(*, final=False):
                    if frontier is None:
                        return
                    db.conn.execute("SAVEPOINT network_checkpoint")
                    try:
                        stored_status = _store(db, site, res, progress, include_partners=False)
                        # Une page devinée sans identité confirmée ne doit pas être
                        # finalisée alors que ses contacts sont encore volontairement retenus.
                        if stored_status != "unverified":
                            _ack_frontier(frontier, res)
                        res.frontier_counts = frontier.counts()
                        if final and stored_status != "unverified":
                            res.status = ("partial" if any(res.frontier_counts[k] for k in
                                          ("pending", "failed", "blocked", "discovery_limited")) else "ok")
                            db.conn.execute("UPDATE sites SET status=? WHERE site_key=?",
                                            (res.status, site["site_key"]))
                        db.conn.execute("RELEASE SAVEPOINT network_checkpoint")
                    except BaseException:
                        db.conn.execute("ROLLBACK TO SAVEPOINT network_checkpoint")
                        db.conn.execute("RELEASE SAVEPOINT network_checkpoint")
                        raise
                    db.commit()
                    new_partners = res.partners - stored_partners
                    if stored_status != "unverified" and new_partners:
                        _store_partners(db, new_partners, progress)
                        stored_partners.update(new_partners)

                try:
                    await asyncio.wait_for(
                        crawler.crawl(site["url"], res, keep_text=keep_text,
                                      deep_pages=deep_pages if go_deep else 0, frontier=frontier,
                                      seed_urls=seeds, checkpoint=checkpoint if frontier else None),
                        site_timeout * 5 if go_deep else site_timeout)
                except asyncio.TimeoutError:
                    res.status = "partial" if go_deep or res.pages else "timeout"
                except asyncio.CancelledError:
                    res.status = "partial" if go_deep or res.pages else "pending"
                    raise
                except Exception as exc:  # un site ne doit jamais arrêter le crawl
                    res.status = "error"
                    log(f"[crawl] erreur sur {site['url']}: {exc!r}")
                finally:
                    if frontier is not None:
                        checkpoint(final=True)
                        waiting = res.frontier_counts.get("pending", 0)
                        if waiting:
                            progress.add("URL restant en file", waiting)
                        for metric, label in (("blocked", "URL exclues par robots"),
                                              ("failed", "URL en échec"),
                                              ("discovery_limited", "découverte limitée")):
                            if res.frontier_counts.get(metric):
                                progress.add(label, res.frontier_counts[metric])
                    else:
                        _store(db, site, res, progress)
                        pending_commit += 1
                        if pending_commit >= 50:
                            db.commit()
                            pending_commit = 0
                    progress.tick()

        ticker = asyncio.create_task(progress.run())
        try:
            await asyncio.gather(*(worker() for _ in range(concurrency)))
        finally:
            ticker.cancel()
            db.commit()
    log(progress.line())


def _ack_frontier(frontier: Frontier, res: CrawlResult) -> None:
    """À appeler uniquement après la sauvegarde des contacts dans la même transaction."""
    frontier.complete(res.completed_urls)
    for url, reason in res.failed_urls.items():
        frontier.fail([url], reason=reason)
    frontier.block(res.blocked_urls)
    res.completed_urls.clear()
    res.failed_urls.clear()
    res.blocked_urls.clear()


def _store(db: DB, site, res: CrawlResult, progress: Progress, *, include_partners=True) -> str:
    key = site["site_key"]
    status = res.status
    emails = res.emails
    phones = res.phones
    entities = res.entities
    matched = False
    candidates = db.conn.execute(
        "SELECT b.*,g.verified AS _verified,g.verification_reason AS _reason "
        "FROM guesses g JOIN businesses b ON b.id=g.business_id WHERE g.site_key=?", (key,)
    ).fetchall()
    for row in candidates:
        business = dict(row)
        if row["_verified"] and row["_reason"]:
            matched = True
            continue
        reason = ""
        match_url = res.final_url or site["url"]
        for entity in entities:
            ok, why = match_identity(business, entity=entity)
            if ok:
                reason = why
                match_url = entity.get("website") or entity.get("source_url") or match_url
                break
        if not reason and not entities:
            # Pas de mélange de coordonnées appartenant à différentes fiches d'un
            # réseau : chaque page doit fournir elle-même une preuve suffisante.
            # Une contradiction dans une entité structurée ne peut pas être
            # annulée par un nom et un code postal dans le HTML autour de celle-ci.
            texts = res.texts if res.is_network else ["\n".join(res.texts)]
            for text in texts:
                ok, why = match_identity(business, text=text)
                if ok:
                    reason = why
                    break
        if reason:
            db.link_guessed(key, match_url, [row["id"]], reason=reason)
            progress.add("domaines devinés confirmés", 1)
            matched = True
    if site["guessed"] and res.pages and not matched:
        status, emails, phones, entities = "unverified", {}, set(), []
    site_domains = {registrable(host_of(site["url"])), registrable(host_of(res.final_url))} - {""}
    new = db.save_crawl(key, status, res.final_url, res.pages, emails, site_domains, phones,
                        entities=entities, is_network=res.is_network)
    if include_partners and status in ("ok", "partial") and res.partners:
        _store_partners(db, res.partners, progress)
    if new:
        progress.add("e-mails", new)
        if not res._reported_email_site:
            progress.add("sites avec e-mail", 1)
            res._reported_email_site = True
    return status


def _store_partners(db: DB, partners: set[str], progress: Progress) -> None:
    added = db.add_businesses([
        {"source": "lien", "source_id": url, "name": host_of(url), "category": "partenaire", "website": url}
        for url in partners
    ])
    if added:
        progress.add("sites partenaires ajoutés", added)
