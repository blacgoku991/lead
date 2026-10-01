"""Stockage SQLite : permet de reprendre un traitement interrompu sans tout refaire."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .extract import clean_email, email_kind, is_role
from .utils import host_of, log, registrable, site_from_url

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
    "country", "phone", "website", "site_key", "siren", "naf", "lat", "lon", "siret",
    "source_url", "source_date", "source_license",
)

_EXTRA_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_business_siret ON businesses(siret);
CREATE INDEX IF NOT EXISTS idx_business_postal ON businesses(postal_code);
CREATE TABLE IF NOT EXISTS observed_networks (
    site_key TEXT PRIMARY KEY, observed_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS business_pages (
    business_id INTEGER NOT NULL, site_key TEXT NOT NULL, url TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT '', PRIMARY KEY (business_id, url)
);
CREATE INDEX IF NOT EXISTS idx_business_pages_site ON business_pages(site_key);
CREATE TABLE IF NOT EXISTS business_sources (
    business_id INTEGER NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '', source_date TEXT, source_license TEXT,
    seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (business_id, source, source_id, source_url)
);
CREATE TABLE IF NOT EXISTS domain_candidates (
    business_id INTEGER NOT NULL, domain TEXT NOT NULL, siret TEXT, data_source TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '', source_date TEXT, source_license TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (business_id, domain, data_source, source_url)
);
CREATE TABLE IF NOT EXISTS attribution_audit (
    id INTEGER PRIMARY KEY, action TEXT NOT NULL, business_id INTEGER, site_key TEXT,
    detail TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


class DB:
    def __init__(self, path: str):
        self.path = str(path)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        existed = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='businesses'").fetchone()
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(businesses)")} if existed else set()
        if existed and "siret" not in columns:
            backup = self.backup("before-v2")
            if backup:
                log(f"[db] sauvegarde avant migration : {backup}")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def backup(self, label: str = "backup") -> str | None:
        """Snapshot SQLite cohérent, y compris le WAL ; aucun fichier utilisateur écrasé."""
        if self.path == ":memory:":
            return None
        self.conn.commit()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        target = str(Path(self.path).resolve()) + f".{label}-{stamp}.db"
        with sqlite3.connect(target) as dest:
            self.conn.backup(dest)
        return target

    def _migrate(self) -> None:
        if self.conn.execute("PRAGMA user_version").fetchone()[0] == 2:
            self.conn.executescript(_EXTRA_SCHEMA)
            return
        additions = {
            "businesses": {"siret": "TEXT", "source_url": "TEXT", "source_date": "TEXT", "source_license": "TEXT"},
            "emails": {"attribution": "TEXT NOT NULL DEFAULT 'unverified'", "evidence": "TEXT NOT NULL DEFAULT ''",
                       "source_type": "TEXT NOT NULL DEFAULT ''", "last_seen_at": "TEXT"},
            "guesses": {"verification_reason": "TEXT NOT NULL DEFAULT ''"},
        }
        for table, fields in additions.items():
            present = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in fields.items():
                if name not in present:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        self.conn.executescript(_EXTRA_SCHEMA)
        self.conn.execute("UPDATE businesses SET siret=source_id WHERE source='sirene' "
                          "AND length(source_id)=14 AND source_id NOT GLOB '*[^0-9]*' "
                          "AND COALESCE(siret,'')=''")
        self.conn.execute("UPDATE emails SET attribution='source', evidence='contact fourni par la source', "
                          "source_type='source' WHERE business_id>0 AND attribution='unverified' "
                          "AND COALESCE(source_type,'')=''")
        self.conn.execute("UPDATE emails SET last_seen_at=found_at WHERE last_seen_at IS NULL")
        self.conn.execute("INSERT OR IGNORE INTO business_pages (business_id,site_key,url,source_type) "
                          "SELECT id,site_key,website,source FROM businesses WHERE site_key IS NOT NULL "
                          "AND COALESCE(website,'')<>''")
        self.conn.execute("PRAGMA user_version=2")
        self.conn.commit()

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
            siret = re.sub(r"\D", "", str(b.get("siret") or ""))
            if not siret and b.get("source") == "sirene":
                siret = str(b.get("source_id") or "")
            siret = siret if re.fullmatch(r"\d{14}", siret) else ""
            b = {**b, "site_key": site_key, "website": url or b.get("website") or "", "siret": siret}
            cur.execute(
                f"INSERT OR IGNORE INTO businesses ({','.join(_BIZ_FIELDS)}) VALUES ({placeholders})",
                [b.get(f) for f in _BIZ_FIELDS],
            )
            added += cur.rowcount
            row = cur.execute("SELECT id FROM businesses WHERE source=? AND source_id=?",
                              (b["source"], b["source_id"])).fetchone()
            bid = row[0]
            self._remember_source(cur, bid, b)
            if site_key:
                self._remember_page(cur, bid, site_key, url, b["source"])
            if b.get("emails"):
                site_domains = {registrable(host_of(url))} if url else set()
                for raw in b["emails"]:
                    self._add_email(cur, raw, site_domains, business_id=bid,
                                    source_url=b.get("source_url") or b["source"], attribution="source",
                                    evidence="contact publié pour cet établissement", source_type=b["source"])
        self.conn.commit()
        return added

    def _remember_source(self, cur, bid: int, b: dict) -> None:
        cur.execute("INSERT INTO business_sources (business_id,source,source_id,source_url,source_date,source_license) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT(business_id,source,source_id,source_url) "
                    "DO UPDATE SET seen_at=CURRENT_TIMESTAMP, source_date=COALESCE(NULLIF(excluded.source_date,''),source_date), "
                    "source_license=COALESCE(NULLIF(excluded.source_license,''),source_license)",
                    (bid, b["source"], b["source_id"], b.get("source_url") or "", b.get("source_date"),
                     b.get("source_license")))

    def _remember_page(self, cur, bid: int, key: str, url: str, source_type: str = "") -> None:
        cur.execute("INSERT OR IGNORE INTO sites (site_key,url) VALUES (?,?)", (key, url))
        cur.execute("INSERT OR IGNORE INTO business_pages (business_id,site_key,url,source_type) VALUES (?,?,?,?)",
                    (bid, key, url, source_type))
        if cur.rowcount:
            # Une nouvelle URL source peut fonctionner même si l'ancienne a échoué.
            # Les pages déjà terminées restent dans la file pour permettre la reprise.
            cur.execute("UPDATE sites SET status='pending' WHERE site_key=?", (key,))

    def upsert_public_businesses(self, items: Iterable[dict]) -> int:
        """Jointure SIRET exacte : enrichit les champs absents et conserve toutes les sources."""
        added = 0
        for original in items:
            b = dict(original)
            siret = re.sub(r"\D", "", str(b.get("siret") or ""))
            b["siret"] = siret if re.fullmatch(r"\d{14}", siret) else ""
            if b["siret"]:
                b.setdefault("siren", siret[:9])
            row = self.conn.execute("SELECT * FROM businesses WHERE source=? AND source_id=?",
                                    (b["source"], b["source_id"])).fetchone()
            if row is None and b["siret"]:
                row = self.conn.execute("SELECT * FROM businesses WHERE siret=? ORDER BY id LIMIT 1", (siret,)).fetchone()
            if row is None:
                added += self.add_businesses([b])
                continue
            cur = self.conn.cursor()
            key, url = site_from_url(b.get("website"))
            for field in _BIZ_FIELDS:
                if field in ("source", "source_id", "site_key", "website"):
                    continue
                value = b.get(field)
                if value is not None and value != "" and (row[field] is None or row[field] == ""):
                    cur.execute(f"UPDATE businesses SET {field}=? WHERE id=?", (value, row["id"]))
            if key and url:
                if not row["website"]:
                    cur.execute("UPDATE businesses SET website=?,site_key=? WHERE id=?", (url, key, row["id"]))
                self._remember_page(cur, row["id"], key, url, b["source"])
            self._remember_source(cur, row["id"], b)
        self.conn.commit()
        return added

    def add_domain_candidates(self, rows: Iterable[dict]) -> int:
        """Un domaine DINUM est une piste à vérifier, jamais un site déjà confirmé."""
        from .config import FREE_EMAIL_DOMAINS
        from .utils import domain_in
        cur = self.conn.cursor()
        added = 0
        for item in rows:
            domain = str(item.get("domain") or "").lower().strip(".")
            key, url = site_from_url(domain)
            if not key or not url or domain_in(domain, FREE_EMAIL_DOMAINS):
                continue
            b = cur.execute("SELECT * FROM businesses WHERE id=?", (item["business_id"],)).fetchone()
            if b is None:
                continue
            known_siret = b["siret"] or (b["source_id"] if b["source"] == "sirene" else "")
            if not re.fullmatch(r"\d{14}", known_siret or "") or known_siret != item.get("siret"):
                continue
            exists = cur.execute("SELECT 1 FROM domain_candidates WHERE business_id=? AND domain=?",
                                 (b["id"], domain)).fetchone()
            cur.execute("INSERT OR IGNORE INTO domain_candidates "
                        "(business_id,domain,siret,data_source,source_url,source_date,source_license) VALUES (?,?,?,?,?,?,?)",
                        (b["id"], domain, known_siret, item.get("data_source") or "", item.get("source_url") or "",
                         item.get("source_date"), item.get("source_license")))
            added += int(not exists and cur.rowcount > 0)
            cur.execute("UPDATE domain_candidates SET source_date=COALESCE(NULLIF(?,''),source_date), "
                        "source_license=COALESCE(NULLIF(?,''),source_license) "
                        "WHERE business_id=? AND domain=? AND data_source=? AND source_url=?",
                        (item.get("source_date"), item.get("source_license"), b["id"], domain,
                         item.get("data_source") or "", item.get("source_url") or ""))
            cur.execute("INSERT OR IGNORE INTO sites (site_key,url,guessed) VALUES (?,?,1)", (key, url))
            tokens = [v for v in (b["postal_code"], b["siren"]) if v]
            cur.execute("INSERT OR IGNORE INTO guesses (business_id,site_key,tokens) VALUES (?,?,?)",
                        (b["id"], key, json.dumps(tokens)))
            if cur.rowcount:
                cur.execute("UPDATE sites SET status='pending' WHERE site_key=? AND status IN ('ok','unverified')", (key,))
        self.conn.commit()
        return added

    # --- sites -------------------------------------------------------------------------------

    def pending_sites(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT site_key, url, guessed FROM sites WHERE status IN ('pending','partial') ORDER BY guessed, rowid"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.conn.execute(sql).fetchall()

    def network_sites(self, min_businesses: int = 2) -> set[str]:
        """Sites partagés par plusieurs établissements (réseaux, franchises, groupes)."""
        from .frontier import is_network_key
        rows = self.conn.execute(
            "SELECT p.site_key FROM business_pages p JOIN businesses b ON b.id=p.business_id "
            "GROUP BY p.site_key HAVING COUNT(DISTINCT CASE WHEN COALESCE(b.siret,'')<>'' "
            "THEN 'siret:'||b.siret ELSE 'id:'||b.id END) >= ?",
            (min_businesses,),
        )
        shared = {r[0] for r in rows}
        shared.update(r[0] for r in self.conn.execute("SELECT site_key FROM sites") if is_network_key(r[0]))
        shared.update(r[0] for r in self.conn.execute("SELECT site_key FROM observed_networks"))
        return shared

    def reset_sites_without_email(self) -> int:
        from .frontier import reset_pages
        keys = [r[0] for r in self.conn.execute(
            "SELECT site_key FROM sites WHERE status='ok' AND site_key NOT IN "
            "(SELECT site_key FROM emails WHERE site_key!='')")]
        for key in keys:
            reset_pages(self.conn, key)
        self.conn.executemany("UPDATE sites SET status='pending' WHERE site_key=?", [(key,) for key in keys])
        self.conn.commit()
        return len(keys)

    def reset_failed_sites(self) -> int:
        from .frontier import ensure_schema, reset_pages
        ensure_schema(self.conn)
        keys = [r[0] for r in self.conn.execute(
            "SELECT site_key FROM sites WHERE status IN ('unreachable','timeout','error') "
            "UNION SELECT site_key FROM crawl_frontier WHERE status='failed'")]
        for key in keys:
            urls = [r[0] for r in self.conn.execute(
                "SELECT url FROM crawl_frontier WHERE site_key=? AND status IN ('pending','failed')", (key,))]
            reset_pages(self.conn, key, urls)
        self.conn.executemany("UPDATE sites SET status='pending' WHERE site_key=?", [(key,) for key in keys])
        self.conn.commit()
        return len(keys)

    def save_crawl(self, site_key: str, status: str, final_url: str, pages: int,
                   emails: dict[str, str], site_domains: set[str], phones: set[str] | None = None,
                   *, entities: list[dict] | None = None, is_network: bool = False) -> int:
        cur = self.conn.cursor()
        if is_network:
            cur.execute("INSERT INTO observed_networks (site_key) VALUES (?) "
                        "ON CONFLICT(site_key) DO UPDATE SET observed_at=CURRENT_TIMESTAMP", (site_key,))
        n = 0
        for email, src in emails.items():
            n += self._add_email(cur, email, site_domains, site_key=site_key, source_url=src,
                                 source_type="page", evidence="adresse publiée ; identité à établir")
        for entity in entities or ():
            n += self._save_entity(cur, site_key, entity, site_domains)
        for phone in phones or ():
            cur.execute("INSERT OR IGNORE INTO site_phones (site_key, phone) VALUES (?, ?)", (site_key, phone))
        cur.execute(
            "UPDATE sites SET status=?, final_url=?, pages=?, crawled_at=CURRENT_TIMESTAMP WHERE site_key=?",
            (status, final_url, pages, site_key),
        )
        return n

    def _save_entity(self, cur, site_key: str, entity: dict, site_domains: set[str]) -> int:
        from .config import refine_category
        from .identity import match_identity
        from .utils import normalized_page_url
        if not entity.get("name") or not entity.get("emails"):
            return 0
        siret = str(entity.get("siret") or "")
        siret = siret if re.fullmatch(r"\d{14}", siret) else ""
        exact = cur.execute("SELECT * FROM businesses WHERE siret=? AND siret<>''", (siret,)).fetchall()
        if exact:
            # L'identifiant exact prime sur les ressemblances nominales et reste
            # contrôlé même si le code postal publié contredit notre ancienne fiche.
            candidates = exact
        else:
            candidate_sql = (
                "SELECT * FROM businesses WHERE id IN (SELECT id FROM businesses WHERE site_key=? "
                "UNION SELECT business_id FROM business_pages WHERE site_key=?")
            candidate_args = [site_key, site_key]
            if entity.get("postal_code"):
                candidate_sql += " UNION SELECT id FROM businesses WHERE postal_code=?)"
                candidate_args.append(entity["postal_code"])
                candidate_sql += " AND (COALESCE(postal_code,'')='' OR postal_code=?)"
                candidate_args.append(entity["postal_code"])
            else:
                candidate_sql += ")"
            candidates = cur.execute(candidate_sql, candidate_args).fetchall()
        matched = []
        for row in candidates:
            ok, reason = match_identity(dict(row), entity=entity)
            if ok:
                matched.append((row, reason))
        if len(matched) > 1:
            identities = {row["siret"] or f"id:{row['id']}" for row, _ in matched}
            if len(identities) > 1:
                return 0  # la publication reste disponible dans les adresses non attribuées
        if matched:
            b, evidence = sorted(matched, key=lambda item: item[0]["id"])[0]
            bid = b["id"]
        else:
            if exact:
                return 0  # contradiction : conserver la publication sans créer un doublon SIRET
            category = entity.get("category")
            identified = bool(siret or (entity.get("address") and entity.get("postal_code")))
            if not category or not identified:
                return 0
            url = entity.get("website") or entity.get("source_url") or ""
            source_id = str(entity.get("source_id") or normalized_page_url(url))
            # Un identifiant de réseau est local à son domaine, contrairement au SIRET.
            source_id = f"{site_key}|{source_id}"
            new_b = {**entity, "source": "network", "source_id": source_id,
                     "website": url, "country": entity.get("country") or "FR",
                     "category": refine_category(category, entity["name"])}
            new_b.pop("emails", None)
            # Évite le commit de add_businesses : la page et son résultat seront validés ensemble.
            key, start = site_from_url(url)
            new_b.update(site_key=key, website=start or url)
            cur.execute(f"INSERT OR IGNORE INTO businesses ({','.join(_BIZ_FIELDS)}) VALUES "
                        f"({','.join('?' for _ in _BIZ_FIELDS)})", [new_b.get(f) for f in _BIZ_FIELDS])
            inserted = cur.rowcount > 0
            current = cur.execute("SELECT * FROM businesses WHERE source='network' AND source_id=?", (source_id,)).fetchone()
            if current is None:
                return 0
            bid = current["id"]
            # Si une fiche existante change d'identité, ne pas confirmer l'ancien contact sans preuve.
            if not inserted:
                ok, _ = match_identity(dict(current), entity=entity)
                if not ok:
                    return 0
            evidence = "identité et contact publiés dans la même fiche structurée"
            self._remember_source(cur, bid, new_b)
        page_url = entity.get("website") or entity.get("source_url")
        key, start = site_from_url(page_url)
        if key and start:
            self._remember_page(cur, bid, key, start, entity.get("source_type") or "structured")
        count = 0
        for raw in entity["emails"]:
            count += self._add_email(cur, raw, site_domains, site_key=site_key, business_id=bid,
                                     source_url=entity.get("source_url") or "", attribution="structured",
                                     evidence=evidence, source_type=entity.get("source_type") or "structured")
        return count

    def _add_email(self, cur, raw: str, site_domains: set[str], *, site_key: str = "",
                   business_id: int = 0, source_url: str = "", attribution: str = "unverified",
                   evidence: str = "", source_type: str = "") -> int:
        email = clean_email(raw)
        if not email:
            return 0
        already_known = cur.execute("SELECT 1 FROM emails WHERE email=? LIMIT 1", (email,)).fetchone()
        cur.execute(
            "INSERT OR IGNORE INTO emails (email, domain, site_key, business_id, kind, is_role, source_url,"
            " attribution,evidence,source_type,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
            (email, email.rsplit("@", 1)[1], site_key, business_id, email_kind(email, site_domains),
             int(is_role(email)), source_url, attribution, evidence, source_type),
        )
        added = cur.rowcount
        cur.execute("UPDATE emails SET last_seen_at=CURRENT_TIMESTAMP, source_url=?,attribution=?,evidence=?,"
                    "source_type=? WHERE email=? AND site_key=? AND business_id=?",
                    (source_url, attribution, evidence, source_type, email, site_key, business_id))
        return int(added > 0 and already_known is None)

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

    def link_guessed(self, site_key: str, url: str, business_ids: list[int], *, reason: str = "") -> None:
        cur = self.conn.cursor()
        for bid in business_ids:
            cur.execute("UPDATE guesses SET verified=1,verification_reason=? WHERE business_id=? AND site_key=?",
                        (reason, bid, site_key))
            cur.execute(
                "UPDATE businesses SET site_key=?, website=? WHERE id=? AND site_key IS NULL",
                (site_key, url, bid),
            )
            self._remember_page(cur, bid, site_key, url, "identity")

    def repair_contacts(self) -> dict:
        """Retire les anciennes confirmations non prouvées, sans supprimer de contacts."""
        from .frontier import reset_pages
        report = {"backup": self.backup("before-repair"), "legacy_guesses_reset": 0,
                  "network_sites_requeued": 0, "source_pages_requeued": 0}
        legacy = self.conn.execute("SELECT g.business_id,g.site_key,b.website,b.site_key AS current_key "
                                   "FROM guesses g JOIN businesses b ON b.id=g.business_id "
                                   "WHERE g.verified=1 AND COALESCE(g.verification_reason,'')=''").fetchall()
        for row in legacy:
            self.conn.execute("INSERT INTO attribution_audit (action,business_id,site_key,detail) VALUES (?,?,?,?)",
                              ("recheck_legacy_guess", row["business_id"], row["site_key"], json.dumps(dict(row))))
            self.conn.execute("UPDATE guesses SET verified=0 WHERE business_id=? AND site_key=?",
                              (row["business_id"], row["site_key"]))
            self.conn.execute("DELETE FROM business_pages WHERE business_id=? AND site_key=? "
                              "AND source_type IN ('identity','sirene')", (row["business_id"], row["site_key"]))
            if row["current_key"] == row["site_key"]:
                self.conn.execute("UPDATE businesses SET site_key=NULL,website='' WHERE id=?",
                                  (row["business_id"],))
            self.conn.execute("UPDATE sites SET status='pending' WHERE site_key=?", (row["site_key"],))
            reset_pages(self.conn, row["site_key"])
            report["legacy_guesses_reset"] += 1
        for key in self.network_sites():
            urls = [r[0] for r in self.conn.execute("SELECT DISTINCT source_url FROM emails WHERE site_key=? "
                                                   "AND COALESCE(source_url,'') LIKE 'http%'", (key,))]
            urls += [r[0] for r in self.conn.execute("SELECT url FROM business_pages WHERE site_key=?", (key,))]
            urls = list(dict.fromkeys(urls))
            reset_pages(self.conn, key, urls or None)
            self.conn.execute("UPDATE sites SET status='pending' WHERE site_key=?", (key,))
            report["network_sites_requeued"] += 1
            report["source_pages_requeued"] += len(urls)
        self.conn.commit()
        return report

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
        result = {
            "entreprises par source": dict(q("SELECT source, COUNT(*) FROM businesses GROUP BY source").fetchall()),
            "entreprises par catégorie": dict(
                q("SELECT category, COUNT(*) FROM businesses GROUP BY category ORDER BY 2 DESC").fetchall()
            ),
            "sites par statut": dict(q("SELECT status, COUNT(*) FROM sites GROUP BY status").fetchall()),
            "e-mails uniques par type": dict(
                q("SELECT kind, COUNT(DISTINCT email) FROM emails GROUP BY kind").fetchall()
            ),
            "e-mails uniques": q("SELECT COUNT(DISTINCT email) FROM emails").fetchone()[0],
            "associations explicites par preuve": dict(q(
                "SELECT attribution,COUNT(*) FROM emails WHERE business_id>0 GROUP BY attribution").fetchall()),
            "liens établissement-domaine candidats": q(
                "SELECT COUNT(*) FROM (SELECT DISTINCT business_id,domain FROM domain_candidates)").fetchone()[0],
            "domaines MX ok / ko / ?": [
                q("SELECT COUNT(*) FROM mx WHERE ok=1").fetchone()[0],
                q("SELECT COUNT(*) FROM mx WHERE ok=0").fetchone()[0],
                q("SELECT COUNT(*) FROM mx WHERE ok IS NULL").fetchone()[0],
            ],
        }
        if q("SELECT 1 FROM sqlite_master WHERE type='table' AND name='crawl_frontier'").fetchone():
            result["pages de crawl par statut"] = dict(q(
                "SELECT status,COUNT(*) FROM crawl_frontier GROUP BY status").fetchall())
        return result
