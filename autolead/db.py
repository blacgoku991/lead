"""Stockage SQLite : permet de reprendre un traitement interrompu sans tout refaire."""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable

from .extract import clean_email, email_kind, is_role
from .utils import host_of, registrable, site_from_url

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    name TEXT, alt_name TEXT, category TEXT,
    address TEXT, postal_code TEXT, city TEXT, country TEXT,
    phone TEXT, website TEXT, site_key TEXT,
    siren TEXT, naf TEXT, lat REAL, lon REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_biz_site ON businesses(site_key);

CREATE TABLE IF NOT EXISTS sites (
    site_key TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    guessed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    final_url TEXT,
    pages INTEGER DEFAULT 0,
    crawled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sites_status ON sites(status);

CREATE TABLE IF NOT EXISTS guesses (
    business_id INTEGER NOT NULL,
    site_key TEXT NOT NULL,
    tokens TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (business_id, site_key)
);
CREATE INDEX IF NOT EXISTS idx_guess_site ON guesses(site_key);

CREATE TABLE IF NOT EXISTS emails (
    email TEXT NOT NULL,
    domain TEXT NOT NULL,
    site_key TEXT NOT NULL DEFAULT '',
    business_id INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL,
    is_role INTEGER NOT NULL DEFAULT 0,
    source_url TEXT,
    found_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (email, site_key, business_id)
);
CREATE INDEX IF NOT EXISTS idx_emails_domain ON emails(domain);
CREATE INDEX IF NOT EXISTS idx_emails_biz ON emails(business_id);

CREATE TABLE IF NOT EXISTS site_phones (
    site_key TEXT NOT NULL,
    phone TEXT NOT NULL,
    PRIMARY KEY (site_key, phone)
);

CREATE TABLE IF NOT EXISTS dns_checked (
    domain TEXT PRIMARY KEY,
    ok INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mx (
    domain TEXT PRIMARY KEY,
    ok INTEGER,
    checked_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

_BIZ_FIELDS = (
    "source", "source_id", "name", "alt_name", "category", "address", "postal_code", "city",
    "country", "phone", "website", "site_key", "siren", "naf", "lat", "lon",
)


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    # --- entreprises -------------------------------------------------------------------------

    def add_businesses(self, items: Iterable[dict]) -> int:
        """Insère des entreprises (+ leur site à crawler, + e-mails déjà connus). Retourne le nb ajouté."""
        cur = self.conn.cursor()
        added = 0
        placeholders = ",".join("?" * len(_BIZ_FIELDS))
        for b in items:
            site_key, url = site_from_url(b.get("website"))
            b = {**b, "site_key": site_key, "website": url or b.get("website") or ""}
            cur.execute(
                f"INSERT OR IGNORE INTO businesses ({','.join(_BIZ_FIELDS)}) VALUES ({placeholders})",
                [b.get(f) for f in _BIZ_FIELDS],
            )
            added += cur.rowcount
            if site_key:
                cur.execute("INSERT OR IGNORE INTO sites (site_key, url) VALUES (?, ?)", (site_key, url))
            if b.get("emails"):
                row = cur.execute(
                    "SELECT id FROM businesses WHERE source=? AND source_id=?", (b["source"], b["source_id"])
                ).fetchone()
                site_domains = {registrable(host_of(url))} if url else set()
                for raw in b["emails"]:
                    self._add_email(cur, raw, site_domains, business_id=row[0], source_url=b["source"])
        self.conn.commit()
        return added

    # --- sites -------------------------------------------------------------------------------

    def pending_sites(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT site_key, url, guessed FROM sites WHERE status='pending' ORDER BY guessed, rowid"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.conn.execute(sql).fetchall()

    def network_sites(self, min_businesses: int = 3) -> set[str]:
        """Sites partagés par plusieurs établissements (réseaux, franchises, groupes)."""
        rows = self.conn.execute(
            "SELECT site_key FROM businesses WHERE site_key IS NOT NULL GROUP BY site_key HAVING COUNT(*) >= ?",
            (min_businesses,),
        )
        return {r[0] for r in rows}

    def reset_sites_without_email(self) -> int:
        cur = self.conn.execute(
            "UPDATE sites SET status='pending' WHERE status='ok' "
            "AND site_key NOT IN (SELECT DISTINCT site_key FROM emails WHERE site_key != '')"
        )
        self.conn.commit()
        return cur.rowcount

    def reset_failed_sites(self) -> int:
        cur = self.conn.execute(
            "UPDATE sites SET status='pending' WHERE status IN ('unreachable','timeout','error')"
        )
        self.conn.commit()
        return cur.rowcount

    def save_crawl(self, site_key: str, status: str, final_url: str, pages: int,
                   emails: dict[str, str], site_domains: set[str], phones: set[str] | None = None) -> int:
        cur = self.conn.cursor()
        n = 0
        for email, src in emails.items():
            n += self._add_email(cur, email, site_domains, site_key=site_key, source_url=src)
        for phone in phones or ():
            cur.execute("INSERT OR IGNORE INTO site_phones (site_key, phone) VALUES (?, ?)", (site_key, phone))
        cur.execute(
            "UPDATE sites SET status=?, final_url=?, pages=?, crawled_at=CURRENT_TIMESTAMP WHERE site_key=?",
            (status, final_url, pages, site_key),
        )
        return n

    def _add_email(self, cur, raw: str, site_domains: set[str], *, site_key: str = "",
                   business_id: int = 0, source_url: str = "") -> int:
        email = clean_email(raw)
        if not email:
            return 0
        cur.execute(
            "INSERT OR IGNORE INTO emails (email, domain, site_key, business_id, kind, is_role, source_url)"
            " VALUES (?,?,?,?,?,?,?)",
            (email, email.rsplit("@", 1)[1], site_key, business_id, email_kind(email, site_domains),
             int(is_role(email)), source_url),
        )
        return cur.rowcount

    # --- domaines devinés --------------------------------------------------------------------

    def businesses_for_guess(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT id, name, alt_name, postal_code, siren, city FROM businesses b "
            "WHERE site_key IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM emails e WHERE e.business_id=b.id)"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.conn.execute(sql).fetchall()

    def dns_checked(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT domain FROM dns_checked")}

    def save_dns(self, results: list[tuple[str, int]]) -> None:
        self.conn.executemany("INSERT OR REPLACE INTO dns_checked (domain, ok) VALUES (?, ?)", results)

    def known_site_keys(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT site_key FROM sites")}

    def add_guesses(self, guesses: dict[str, list[tuple[int, list[str]]]]) -> None:
        cur = self.conn.cursor()
        for domain, owners in guesses.items():
            cur.execute(
                "INSERT OR IGNORE INTO sites (site_key, url, guessed) VALUES (?, ?, 1)",
                (domain, f"https://{domain}/"),
            )
            for bid, tokens in owners:
                cur.execute(
                    "INSERT OR IGNORE INTO guesses (business_id, site_key, tokens) VALUES (?,?,?)",
                    (bid, domain, json.dumps(tokens)),
                )
        self.conn.commit()

    def guess_owners(self, site_key: str) -> list[tuple[int, list[str]]]:
        rows = self.conn.execute("SELECT business_id, tokens FROM guesses WHERE site_key=?", (site_key,))
        return [(r[0], json.loads(r[1])) for r in rows]

    def link_guessed(self, site_key: str, url: str, business_ids: list[int]) -> None:
        cur = self.conn.cursor()
        for bid in business_ids:
            cur.execute("UPDATE guesses SET verified=1 WHERE business_id=? AND site_key=?", (bid, site_key))
            cur.execute(
                "UPDATE businesses SET site_key=?, website=? WHERE id=? AND site_key IS NULL",
                (site_key, url, bid),
            )

    # --- MX ----------------------------------------------------------------------------------

    def domains_to_check(self, recheck: bool = False) -> list[str]:
        if recheck:
            sql = "SELECT DISTINCT domain FROM emails"
        else:
            sql = ("SELECT DISTINCT e.domain FROM emails e LEFT JOIN mx m ON m.domain=e.domain "
                   "WHERE m.domain IS NULL OR m.ok IS NULL")
        return [r[0] for r in self.conn.execute(sql)]

    def save_mx(self, results: list[tuple[str, int | None]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO mx (domain, ok, checked_at) VALUES (?, ?, CURRENT_TIMESTAMP)", results
        )
        self.conn.commit()

    # --- stats -------------------------------------------------------------------------------

    def stats(self) -> dict:
        q = self.conn.execute
        return {
            "entreprises par source": dict(q("SELECT source, COUNT(*) FROM businesses GROUP BY source").fetchall()),
            "entreprises par catégorie": dict(
                q("SELECT category, COUNT(*) FROM businesses GROUP BY category ORDER BY 2 DESC").fetchall()
            ),
            "sites par statut": dict(q("SELECT status, COUNT(*) FROM sites GROUP BY status").fetchall()),
            "e-mails uniques par type": dict(
                q("SELECT kind, COUNT(DISTINCT email) FROM emails GROUP BY kind").fetchall()
            ),
            "e-mails uniques": q("SELECT COUNT(DISTINCT email) FROM emails").fetchone()[0],
            "domaines MX ok / ko / ?": [
                q("SELECT COUNT(*) FROM mx WHERE ok=1").fetchone()[0],
                q("SELECT COUNT(*) FROM mx WHERE ok=0").fetchone()[0],
                q("SELECT COUNT(*) FROM mx WHERE ok IS NULL").fetchone()[0],
            ],
        }
