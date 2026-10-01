"""File persistante des URL de réseaux, indépendante de l'identité des garages.

Les méthodes ne valident jamais une transaction : l'appelant enregistre les contacts,
puis marque les pages terminées et valide les deux écritures ensemble. Une page
interrompue reste ainsi disponible au passage suivant.
"""
from __future__ import annotations

import sqlite3
from urllib.parse import urlsplit, urlunsplit


NETWORK_DOMAINS = frozenset({
    "top-garage.fr", "motrio.fr", "motrio.com", "axial.org", "five-star.fr",
})


def is_network_key(key: str) -> bool:
    """Reconnaît également les sous-domaines et les clés avec un port/chemin."""
    try:
        host = (urlsplit(key if "://" in key else "https://" + key).hostname or "").lower()
    except ValueError:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in NETWORK_DOMAINS)


def canonical_url(url: str) -> str | None:
    """Garde le chemin ET la requête ; seul le fragment de navigation est retiré."""
    try:
        p = urlsplit(url.strip())
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            return None
        # Accéder à .port valide aussi les valeurs mal formées.
        port = p.port
        host = p.hostname.lower().rstrip(".")
        if ":" in host:
            host = f"[{host}]"
        authority = host
        if port is not None and (p.scheme, port) not in (("http", 80), ("https", 443)):
            authority += f":{port}"
        return urlunsplit((p.scheme.lower(), authority, p.path or "/", p.query, ""))
    except (AttributeError, TypeError, ValueError):
        return None


def ensure_schema(conn: sqlite3.Connection) -> None:
    # Pas d'executescript : il pourrait valider des contacts encore en cours d'écriture.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crawl_frontier (
            site_key TEXT NOT NULL,
            url TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'page',
            priority INTEGER NOT NULL DEFAULT 50,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            discovered_from TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (site_key, url)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_frontier_pending ON crawl_frontier(site_key,status,kind,priority)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crawl_frontier_meta (
            site_key TEXT PRIMARY KEY,
            discovery_limited INTEGER NOT NULL DEFAULT 0
        )
    """)


class Frontier:
    def __init__(self, conn: sqlite3.Connection, site_key: str, *, max_urls: int = 100_000):
        self.conn, self.site_key, self.max_urls = conn, site_key, max_urls
        ensure_schema(conn)

    def enqueue(self, urls, *, kind: str = "page", priority: int = 50,
                discovered_from: str = "") -> int:
        if kind not in ("page", "sitemap"):
            raise ValueError("kind doit être page ou sitemap")
        existing = self.conn.execute(
            "SELECT COUNT(*) FROM crawl_frontier WHERE site_key=?", (self.site_key,)
        ).fetchone()[0]
        added = 0
        for raw in urls:
            url = canonical_url(raw)
            if not url:
                continue
            row = self.conn.execute(
                "SELECT 1 FROM crawl_frontier WHERE site_key=? AND url=?", (self.site_key, url)
            ).fetchone()
            if row:
                self.conn.execute(
                    "UPDATE crawl_frontier SET priority=MIN(priority,?) WHERE site_key=? AND url=?",
                    (priority, self.site_key, url),
                )
                continue
            if existing + added >= self.max_urls:
                self.mark_limited()
                break
            self.conn.execute(
                "INSERT INTO crawl_frontier(site_key,url,kind,priority,discovered_from) VALUES(?,?,?,?,?)",
                (self.site_key, url, kind, priority, discovered_from),
            )
            added += 1
        return added

    def pending(self, limit: int = 60, *, kind: str | None = None, exclude=()) -> list[dict]:
        sql = "SELECT rowid,url,kind,priority,attempts FROM crawl_frontier WHERE site_key=? AND status='pending'"
        args = [self.site_key]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        excluded = sorted(set(exclude))
        if excluded:
            sql += f" AND url NOT IN ({','.join('?' for _ in excluded)})"
            args.extend(excluded)
        sql += " ORDER BY priority,rowid LIMIT ?"
        args.append(max(0, limit))
        rows = self.conn.execute(sql, args)
        return [dict(zip(("rowid", "url", "kind", "priority", "attempts"), r)) for r in rows]

    def complete(self, urls) -> None:
        self.conn.executemany(
            "UPDATE crawl_frontier SET status='done',last_error='',updated_at=CURRENT_TIMESTAMP "
            "WHERE site_key=? AND url=?",
            ((self.site_key, url) for raw in urls if (url := canonical_url(raw))),
        )

    def fail(self, urls, *, reason: str = "fetch_failed", max_attempts: int = 3) -> None:
        self.conn.executemany(
            "UPDATE crawl_frontier SET attempts=attempts+1,"
            "status=CASE WHEN attempts+1>=? THEN 'failed' ELSE 'pending' END,"
            "last_error=?,updated_at=CURRENT_TIMESTAMP WHERE site_key=? AND url=?",
            ((max_attempts, reason, self.site_key, url) for raw in urls if (url := canonical_url(raw))),
        )

    def block(self, urls, *, reason: str = "robots_disallow") -> None:
        self.conn.executemany(
            "UPDATE crawl_frontier SET status='blocked',last_error=?,updated_at=CURRENT_TIMESTAMP "
            "WHERE site_key=? AND url=?",
            ((reason, self.site_key, url) for raw in urls if (url := canonical_url(raw))),
        )

    def mark_limited(self) -> None:
        self.conn.execute(
            "INSERT INTO crawl_frontier_meta(site_key,discovery_limited) VALUES(?,1) "
            "ON CONFLICT(site_key) DO UPDATE SET discovery_limited=1", (self.site_key,)
        )

    def counts(self) -> dict[str, int]:
        counts = {"pending": 0, "done": 0, "failed": 0, "blocked": 0}
        counts.update(dict(self.conn.execute(
            "SELECT status,COUNT(*) FROM crawl_frontier WHERE site_key=? GROUP BY status", (self.site_key,)
        )))
        row = self.conn.execute(
            "SELECT discovery_limited FROM crawl_frontier_meta WHERE site_key=?", (self.site_key,)
        ).fetchone()
        counts["discovery_limited"] = row[0] if row else 0
        return counts


def reset_pages(conn: sqlite3.Connection, site_key: str, urls=None) -> int:
    """Reprogramme toutes les pages ou seulement les sources à réparer, sans commit.

    Les URL fournies mais encore inconnues sont ajoutées. Les autres pages restent
    terminées, ce qui permet un retraitement ciblé sans relancer tout un réseau.
    """
    frontier = Frontier(conn, site_key)
    if urls is None:
        cur = conn.execute(
            "UPDATE crawl_frontier SET status='pending',attempts=0,last_error='',"
            "updated_at=CURRENT_TIMESTAMP WHERE site_key=?", (site_key,)
        )
        count = cur.rowcount
    else:
        clean = sorted({url for raw in urls if (url := canonical_url(raw))})
        frontier.enqueue(clean, priority=0)
        count = 0
        for url in clean:
            cur = conn.execute(
                "UPDATE crawl_frontier SET status='pending',attempts=0,last_error='',"
                "priority=0,updated_at=CURRENT_TIMESTAMP WHERE site_key=? AND url=?",
                (site_key, url),
            )
            count += cur.rowcount
    # Cette fonction est également utilisable sur une connexion sans table sites.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sites'").fetchone():
        conn.execute("UPDATE sites SET status='pending' WHERE site_key=?", (site_key,))
    return count
