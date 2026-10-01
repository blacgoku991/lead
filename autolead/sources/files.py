"""Source fichier : vos propres listes de sites (un par ligne, ou CSV avec une colonne site/url/domaine)."""
from __future__ import annotations

import csv

from ..db import DB
from ..utils import log, site_from_url

_URL_COLS = ("website", "site", "site_web", "siteweb", "url", "domain", "domaine", "web")
_NAME_COLS = ("name", "nom", "entreprise", "raison_sociale", "societe", "company")
_CITY_COLS = ("city", "ville", "commune")
_CP_COLS = ("postal_code", "code_postal", "cp", "zip")


def _pick(row: dict, cols) -> str:
    lowered = {(k or "").strip().lower().replace(" ", "_").replace("-", "_"): v for k, v in row.items()}
    return next(((lowered[c] or "").strip() for c in cols if lowered.get(c)), "")


def collect_file(db: DB, path: str, category: str = "autre") -> int:
    items = []
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        if path.lower().endswith(".csv"):
            sample = fh.read(4096)
            fh.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            rows = [(_pick(r, _URL_COLS), r) for r in csv.DictReader(fh, dialect=dialect)]
        else:
            rows = [(line.strip(), {}) for line in fh if line.strip() and not line.startswith("#")]
    for url, row in rows:
        key, start = site_from_url(url)
        if not key:
            continue
        items.append({
            "source": "file", "source_id": key, "name": _pick(row, _NAME_COLS) or key,
            "category": category, "city": _pick(row, _CITY_COLS), "postal_code": _pick(row, _CP_COLS),
            "website": start,
        })
    added = db.add_businesses(items)
    log(f"[file] {path} : {len(items)} sites lus, {added} nouveaux")
    return added
