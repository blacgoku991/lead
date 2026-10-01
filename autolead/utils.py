"""Utilitaires partagés : domaines, URL, limiteur de débit, affichage de progression."""
from __future__ import annotations

import asyncio
import re
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import BLOCKED_SITE_DOMAINS

_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
# Second niveau réservé sous un ccTLD : garage.co.uk, mairie.gouv.fr...
_SLD = {"co", "com", "org", "net", "gouv", "asso", "ac", "gov", "edu", "nom", "ltd", "plc"}
# Hébergeurs où plusieurs sites partagent le même hôte (le chemin les distingue)
SHARED_HOSTS = {"sites.google.com"}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def registrable(host: str) -> str:
    """Domaine "enregistrable" approximatif : www.garage-dupont.fr -> garage-dupont.fr."""
    host = (host or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    if _IP_RE.match(host):
        return host
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if len(parts[-1]) == 2 and parts[-2] in _SLD:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def domain_in(host: str, domains) -> bool:
    """Vrai si `host` est l'un des `domains` ou un de leurs sous-domaines."""
    h = host
    while h:
        if h in domains:
            return True
        _, _, h = h.partition(".")
    return False


def normalized_page_url(url: str) -> str:
    """Clé de fiche : conserve le chemin et les paramètres d'identité, ignore le tracking."""
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower().rstrip(".")
        host = host[4:] if host.startswith("www.") else host
        port = p.port
        if port and port not in (80, 443):
            host += f":{port}"
        params = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                  if not k.lower().startswith("utm_") and k.lower() not in ("gclid", "fbclid", "msclkid")]
        query = urlencode(sorted(params))
        return host + (p.path.rstrip("/") or "/") + ("?" + query if query else "")
    except (ValueError, TypeError):
        return ""


def site_from_url(raw: str | None) -> tuple[str | None, str | None]:
    """Normalise un site web -> (clé unique du site, URL de départ). (None, None) si inutilisable."""
    if not raw:
        return None, None
    raw = re.split(r"[\s;,|]+", raw.strip())[0]
    if not raw:
        return None, None
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    try:
        p = urlsplit(raw)
        host = (p.hostname or "").lower().rstrip(".")
        port = p.port
    except ValueError:
        return None, None
    if p.scheme not in ("http", "https") or "." not in host or p.username or p.password:
        return None, None
    if host not in SHARED_HOSTS and domain_in(host, BLOCKED_SITE_DOMAINS):
        return None, None
    key = host[4:] if host.startswith("www.") else host
    if port and port not in (80, 443):
        key += f":{port}"
    if host in SHARED_HOSTS:
        segs = [s for s in p.path.split("/") if s]
        if not segs:
            return None, None
        key = f"{host}/{'/'.join(segs[:2])}"
    return key, urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, ""))


class RateLimiter:
    """Limite le nombre de requêtes par seconde (partagé entre coroutines)."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        if not self.interval:
            return
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
                now = self._next
            self._next = now + self.interval


class Progress:
    """Affiche périodiquement l'avancement : compteur, débit, ETA."""

    def __init__(self, label: str, total: int | None = None, every: float = 5.0):
        self.label = label
        self.total = total
        self.every = every
        self.done = 0
        self.counters: dict[str, int] = {}
        self.start = time.monotonic()

    def tick(self, n: int = 1) -> None:
        self.done += n

    def add(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def line(self) -> str:
        elapsed = max(time.monotonic() - self.start, 1e-6)
        rate = self.done / elapsed
        parts = [f"[{self.label}] {self.done}" + (f"/{self.total}" if self.total else "")]
        parts.append(f"{rate:.1f}/s")
        parts += [f"{k}: {v}" for k, v in self.counters.items()]
        if self.total and rate > 0:
            remaining = (self.total - self.done) / rate
            parts.append(f"reste ~{int(remaining // 60)}m{int(remaining % 60):02d}s")
        return " | ".join(parts)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.every)
            log(self.line())
