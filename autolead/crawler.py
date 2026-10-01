"""Crawler asynchrone : visite la page d'accueil + contact / mentions légales de chaque site.

Des centaines de sites sont traités en parallèle ; chaque site est limité à quelques pages
et quelques requêtes simultanées pour rester rapide et ne pas surcharger les petits serveurs.
"""
from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import aiohttp

from .db import DB
from .extract import discover_external_sites, discover_links, extract_emails, extract_phones
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


@dataclass
class CrawlResult:
    status: str = "pending"
    final_url: str = ""
    pages: int = 0
    emails: dict = field(default_factory=dict)  # e-mail -> URL de la page où il a été trouvé
    phones: set = field(default_factory=set)
    texts: list = field(default_factory=list)  # contenu des pages (vérification des domaines devinés)
    partners: set = field(default_factory=set)  # sites auto externes cités (partenaires, réseau...)


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
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return f"{host}{p.path.rstrip('/')}?{p.query}"


class Crawler:
    def __init__(self, session: aiohttp.ClientSession, *, max_pages: int = 10, max_bytes: int = 1_500_000,
                 user_agent: str = DEFAULT_UA, per_site_parallel: int = 3, respect_robots: bool = True,
                 follow_partners: bool = True):
        self.session = session
        self.max_pages = max_pages
        self.max_bytes = max_bytes
        self.user_agent = user_agent
        self.per_site_parallel = per_site_parallel
        self.respect_robots = respect_robots
        self.follow_partners = follow_partners

    async def crawl(self, url: str, res: CrawlResult, keep_text: bool = False) -> CrawlResult:
        robots: dict[str, RobotFileParser | None] = {}
        home = None
        for candidate in self._home_candidates(url):
            home = await self._fetch(candidate, robots)
            if home:
                break
        if home is None:
            res.status = "unreachable"
            return res
        res.final_url = home.url
        self._consume(home, res, keep_text)

        seen = {_norm(home.url), _norm(url)}
        links = [u for u in discover_links(home.text, home.url) if _norm(u) not in seen]
        pages = await self._fetch_batch(links[: self.max_pages - 1], res, seen, robots, keep_text)

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

    async def _fetch(self, url: str, robots: dict) -> Page | None:
        if not await self._allowed(url, robots):
            return None
        try:
            async with self.session.get(url, allow_redirects=True, max_redirects=6) as r:
                if r.status >= 400:
                    return None
                ctype = r.headers.get("Content-Type", "").lower()
                if ctype and not any(t in ctype for t in ("html", "text/plain", "xml")):
                    return None
                chunks, size = [], 0
                async for chunk in r.content.iter_chunked(65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= self.max_bytes:
                        break
                return Page(str(r.url), _decode(b"".join(chunks), r.charset))
        except _NETWORK_ERRORS:
            return None

    async def _allowed(self, url: str, robots: dict) -> bool:
        if not self.respect_robots:
            return True
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in robots:
            robots[origin] = await self._load_robots(origin)
        rp = robots[origin]
        return rp is None or rp.can_fetch(self.user_agent, url)

    async def _load_robots(self, origin: str) -> RobotFileParser | None:
        try:
            async with self.session.get(origin + "/robots.txt", allow_redirects=True,
                                        timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status in (401, 403):
                    rp = RobotFileParser()
                    rp.disallow_all = True
                    return rp
                if r.status >= 400:
                    return None
                body = await r.content.read(300_000)
        except _NETWORK_ERRORS:
            return None
        rp = RobotFileParser()
        rp.parse(body.decode("utf-8", "replace").splitlines())
        return rp


def verify_tokens(text: str, tokens: list[str]) -> bool:
    """Le site deviné appartient-il bien à l'entreprise ? (code postal ou SIREN présent dans les pages)"""
    digits = re.sub(r"(?<=\d)[\s. -](?=\d)", "", text)
    for tok in tokens:
        src = digits if len(tok) == 9 else text
        if re.search(rf"(?<!\d){re.escape(tok)}(?!\d)", src):
            return True
    return False


async def run_crawl(db: DB, *, concurrency: int = 150, max_pages: int = 10, timeout: float = 15.0,
                    site_timeout: float = 60.0, user_agent: str = DEFAULT_UA, limit: int | None = None,
                    trust_env: bool = True, follow_partners: bool = True, rounds: int = 3) -> None:
    # getaddrinfo tourne dans un pool de threads : on l'agrandit pour ne pas brider le DNS
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=min(256, concurrency + 16)))
    # Chaque tour visite les sites en attente, dont les sites partenaires découverts au tour précédent
    for n in range(1, (rounds if follow_partners and not limit else 1) + 1):
        sites = db.pending_sites(limit)
        if not sites:
            if n == 1:
                log("[crawl] aucun site en attente.")
            return
        label = "" if n == 1 else f" (tour {n} : sites partenaires découverts)"
        log(f"[crawl] {len(sites)} sites à visiter{label}, {concurrency} en parallèle")
        await _crawl_round(db, sites, concurrency=concurrency, max_pages=max_pages, timeout=timeout,
                           site_timeout=site_timeout, user_agent=user_agent, trust_env=trust_env,
                           follow_partners=follow_partners)


async def _crawl_round(db: DB, sites, *, concurrency, max_pages, timeout, site_timeout, user_agent,
                       trust_env, follow_partners) -> None:
    queue: asyncio.Queue = asyncio.Queue()
    for s in sites:
        queue.put_nowait(s)
    progress = Progress("crawl", total=len(sites))
    pending_commit = 0

    async with make_session(concurrency=concurrency * 3, per_host=4, timeout=timeout,
                            user_agent=user_agent, trust_env=trust_env) as session:
        crawler = Crawler(session, max_pages=max_pages, user_agent=user_agent, follow_partners=follow_partners)

        async def worker() -> None:
            nonlocal pending_commit
            while True:
                try:
                    site = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                res = CrawlResult()
                try:
                    await asyncio.wait_for(crawler.crawl(site["url"], res, keep_text=bool(site["guessed"])),
                                           site_timeout)
                except asyncio.TimeoutError:
                    res.status = "ok" if res.pages else "timeout"
                except Exception as exc:  # un site ne doit jamais arrêter le crawl
                    res.status = "error"
                    log(f"[crawl] erreur sur {site['url']}: {exc!r}")
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


def _store(db: DB, site, res: CrawlResult, progress: Progress) -> None:
    key = site["site_key"]
    status = res.status
    emails = res.emails
    phones = res.phones
    if site["guessed"] and status == "ok":
        text = "\n".join(res.texts)
        matched = [bid for bid, tokens in db.guess_owners(key) if verify_tokens(text, tokens)]
        if matched:
            db.link_guessed(key, res.final_url or site["url"], matched)
            progress.add("domaines devinés confirmés", 1)
        else:
            status, emails, phones = "unverified", {}, set()
    site_domains = {registrable(host_of(site["url"])), registrable(host_of(res.final_url))} - {""}
    new = db.save_crawl(key, status, res.final_url, res.pages, emails, site_domains, phones)
    if status == "ok" and res.partners:
        added = db.add_businesses([
            {"source": "lien", "source_id": url, "name": host_of(url), "category": "partenaire",
             "website": url} for url in res.partners
        ])
        if added:
            progress.add("sites partenaires ajoutés", added)
    if new:
        progress.add("e-mails", new)
        progress.add("sites avec e-mail", 1)
