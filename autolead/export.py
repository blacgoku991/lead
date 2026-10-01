"""Export CSV (compatible Excel FR : séparateur ';', UTF-8 avec BOM)."""
from __future__ import annotations

import csv

from .db import DB
from .utils import log

COLUMNS = [
    "email", "type_email", "generique", "mx", "entreprise", "categorie", "adresse", "code_postal",
    "ville", "telephone", "site_web", "siren", "naf", "source", "page_source",
]
_KIND_RANK = {"domaine_site": 0, "pro": 1, "gratuit": 2}


def export_csv(db: DB, path: str, *, pro_only: bool = False, mx_only: bool = False,
               categories: list[str] | None = None, dedupe: bool = True, sep: str = ";") -> int:
    conn = db.conn
    by_id = {r["id"]: r for r in conn.execute(
        "SELECT * FROM businesses WHERE id IN (SELECT business_id FROM emails)"
        " OR site_key IN (SELECT site_key FROM emails WHERE site_key != '')"
        " OR id IN (SELECT business_id FROM guesses WHERE verified=1)"
    )}
    by_site: dict = {}
    for b in by_id.values():
        if b["site_key"]:
            by_site.setdefault(b["site_key"], b)
    for bid, key in conn.execute("SELECT business_id, site_key FROM guesses WHERE verified=1"):
        if key not in by_site and bid in by_id:
            by_site[key] = by_id[bid]
    mx = dict(conn.execute("SELECT domain, ok FROM mx").fetchall())

    rows = []
    for e in conn.execute("SELECT * FROM emails"):
        b = by_id.get(e["business_id"]) if e["business_id"] else by_site.get(e["site_key"])
        if pro_only and e["kind"] == "gratuit":
            continue
        ok = mx.get(e["domain"])
        if mx_only and ok != 1:
            continue
        if categories and (not b or b["category"] not in categories):
            continue
        rows.append({
            "email": e["email"],
            "type_email": e["kind"],
            "generique": "oui" if e["is_role"] else "non",
            "mx": {1: "ok", 0: "invalide"}.get(ok, "non vérifié"),
            "entreprise": b["name"] if b else "",
            "categorie": b["category"] if b else "",
            "adresse": b["address"] if b else "",
            "code_postal": b["postal_code"] if b else "",
            "ville": b["city"] if b else "",
            "telephone": b["phone"] if b else "",
            "site_web": (b["website"] if b else "") or (f"https://{e['site_key']}/" if e["site_key"] else ""),
            "siren": b["siren"] if b else "",
            "naf": b["naf"] if b else "",
            "source": b["source"] if b else "",
            "page_source": e["source_url"] or "",
        })

    rows.sort(key=lambda r: (_KIND_RANK.get(r["type_email"], 9), r["entreprise"] == "", r["email"]))
    if dedupe:
        seen, unique = set(), []
        for r in rows:
            if r["email"] not in seen:
                seen.add(r["email"])
                unique.append(r)
        rows = unique
    rows.sort(key=lambda r: (r["categorie"], r["code_postal"], r["entreprise"], _KIND_RANK.get(r["type_email"], 9)))

    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, delimiter=sep)
        writer.writeheader()
        writer.writerows(rows)
    log(f"[export] {len(rows)} lignes -> {path}")
    return len(rows)
