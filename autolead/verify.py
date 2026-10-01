"""Vérifie que le domaine de chaque e-mail peut recevoir du courrier (enregistrement MX)."""
from __future__ import annotations

import asyncio

import dns.asyncresolver
import dns.exception
import dns.resolver

from .db import DB
from .utils import Progress, log


async def mx_status(resolver, domain: str) -> int | None:
    """1 = peut recevoir des e-mails, 0 = non, None = inconnu (timeout)."""
    try:
        answer = await resolver.resolve(domain, "MX")
        hosts = [r.exchange.to_text().rstrip(".") for r in answer]
        return 1 if any(hosts) else 0  # "MX 0 ." = domaine qui refuse explicitement le courrier
    except dns.resolver.NXDOMAIN:
        return 0
    except dns.resolver.NoAnswer:
        try:
            await resolver.resolve(domain, "A")  # MX implicite
            return 1
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return 0
        except dns.exception.DNSException:
            return None
    except dns.exception.DNSException:
        return None


async def run_verify(db: DB, *, concurrency: int = 200, recheck: bool = False) -> None:
    domains = db.domains_to_check(recheck)
    if not domains:
        log("[verify] rien à vérifier.")
        return
    try:
        resolver = dns.asyncresolver.Resolver()
    except dns.exception.DNSException as exc:
        log(f"[verify] résolveur DNS indisponible : {exc!r}")
        return
    resolver.timeout, resolver.lifetime = 3.0, 8.0
    sem = asyncio.Semaphore(concurrency)
    progress = Progress("verify MX", total=len(domains))
    results: list[tuple[str, int | None]] = []

    async def check(d: str) -> None:
        async with sem:
            status = await mx_status(resolver, d)
        results.append((d, status))
        progress.add({1: "ok", 0: "ko"}.get(status, "?"), 1)
        progress.tick()

    ticker = asyncio.create_task(progress.run())
    try:
        await asyncio.gather(*(check(d) for d in domains))
    finally:
        ticker.cancel()
    db.save_mx(results)
    log(progress.line())
