"""Exports avec provenance et rattachement explicite, compatibles Excel FR."""
from __future__ import annotations

import csv
from collections import defaultdict

from .db import DB
from .utils import log

COLUMNS = [
    "email", "type_email", "generique", "mx", "entreprise", "categorie", "adresse", "code_postal",
    "ville", "telephone", "telephone_site", "site_web", "siren", "naf", "source", "page_source",
    "siret", "etat_rattachement", "preuve_rattachement", "type_source_contact", "date_collecte",
    "derniere_verification", "nombre_etablissements",
]
_KIND_RANK = {"domaine_site": 0, "pro": 1, "gratuit": 2}
_ATTR_RANK = {"source": 0, "structured": 0, "site_unique": 1, "unverified": 9}


def _context(db: DB, *, include_phone_sites: bool = False):
    """Charge seulement les identités utiles aux contacts de cet export.

    Les entreprises sans contact restent en base. L'export des téléphones
    parcourt leurs fiches séparément et demande aussi les sites à téléphone seul.
    """
    conn = db.conn
    site_sql = "SELECT DISTINCT site_key FROM emails WHERE site_key<>''"
    if include_phone_sites:
        site_sql += " UNION SELECT site_key FROM site_phones WHERE site_key<>''"
    cte = f"WITH contact_sites AS ({site_sql}) "
    by_id = {r["id"]: dict(r) for r in conn.execute(
        cte + "SELECT * FROM businesses WHERE id IN ("
        "SELECT business_id FROM emails WHERE business_id>0 "
        "UNION SELECT id FROM businesses WHERE site_key IN (SELECT site_key FROM contact_sites) "
        "UNION SELECT business_id FROM business_pages "
        "WHERE site_key IN (SELECT site_key FROM contact_sites))"
    )}
    by_site = defaultdict(dict)
    guessed = {r[0]: r[1] for r in conn.execute(
        cte + "SELECT c.site_key,s.guessed FROM contact_sites c LEFT JOIN sites s ON s.site_key=c.site_key"
    )}
    legacy, verified = set(), set()
    for bid, key, reason in conn.execute(
        cte + "SELECT business_id,site_key,verification_reason FROM guesses "
        "WHERE verified=1 AND site_key IN (SELECT site_key FROM contact_sites)"
    ):
        (verified if reason else legacy).add((bid, key))
    for bid, key in conn.execute(
        cte + "SELECT DISTINCT business_id,site_key FROM business_pages "
        "WHERE site_key IN (SELECT site_key FROM contact_sites)"
    ):
        if bid in by_id and (bid, key) not in legacy:
            if not guessed.get(key) or (bid, key) in verified:
                by_site[key][bid] = by_id[bid]
    for b in by_id.values():
        key = b["site_key"]
        if key in guessed and (b["id"], key) not in legacy:
            if not guessed.get(key) or (b["id"], key) in verified:
                by_site[key][b["id"]] = b
    networks = db.network_sites() if by_site else set()
    unique = {}
    for key, items in by_site.items():
        identities = {("siret", b["siret"]) if b["siret"] else ("id", b["id"]) for b in items.values()}
        if key in networks or len(identities) != 1:
            continue
        # Une URL seulement découverte chez un partenaire n'est pas une identité prouvée.
        eligible = [b for b in items.values() if b["source"] not in ("lien", "search") and b["name"]]
        if eligible:
            unique[key] = min(eligible, key=lambda b: b["id"])
    phones = defaultdict(list)
    for key, phone in conn.execute(
        cte + "SELECT site_key,phone FROM site_phones "
        "WHERE site_key IN (SELECT site_key FROM contact_sites) ORDER BY phone"
    ):
        phones[key].append(phone)
    return by_id, unique, phones


def _associated_contacts(db: DB, *, include_phone_sites: bool = False):
    by_id, unique, phones = _context(db, include_phone_sites=include_phone_sites)
    explicit = {(r[0], r[1]) for r in db.conn.execute(
        "SELECT DISTINCT e.email,e.site_key FROM emails e JOIN businesses b ON b.id=e.business_id "
        "WHERE e.attribution IN ('source','structured')"
    )}
    contacts = []
    for raw in db.conn.execute("SELECT * FROM emails ORDER BY rowid"):
        e = dict(raw)
        b = by_id.get(e["business_id"]) if e["attribution"] in ("source", "structured") else None
        attribution, evidence = e["attribution"], e["evidence"]
        # Une fiche de partenaire explique son contact avant toute attribution
        # par le domaine hôte. La même adresse sur un autre site reste indépendante.
        if b is None and (e["email"], e["site_key"]) in explicit:
            continue
        if b is None and not e["business_id"] and e["site_key"] in unique:
            b = unique[e["site_key"]]
            attribution, evidence = "site_unique", "site indépendant associé à un seul établissement"
        if b is None:
            attribution = "unverified"
        contacts.append((e, b, attribution, evidence))
    return contacts, unique, phones


def export_csv(db: DB, path: str, *, pro_only: bool = False, mx_only: bool = False,
               categories: list[str] | None = None, dedupe: bool = True, sep: str = ";",
               attributed_only: bool = False) -> int:
    contacts, unique, site_phones = _associated_contacts(db)
    mx = {r[0]: (r[1], r[2]) for r in db.conn.execute("SELECT domain,ok,checked_at FROM mx")}
    associations = defaultdict(set)
    for e, b, _, _ in contacts:
        if b:
            associations[e["email"]].add(b["siret"] or f"id:{b['id']}")
    rows = []
    for e, b, attribution, evidence in contacts:
        if pro_only and e["kind"] == "gratuit":
            continue
        ok, checked_at = mx.get(e["domain"], (None, ""))
        if mx_only and ok != 1:
            continue
        if attributed_only and b is None:
            continue
        if categories and (not b or b["category"] not in categories):
            continue
        field = lambda key: (b.get(key) or "") if b else ""
        key = e["site_key"]
        safe_site_phones = site_phones.get(key, []) if b and unique.get(key, {}).get("id") == b["id"] else []
        rows.append({
            "email": e["email"], "type_email": e["kind"], "generique": "oui" if e["is_role"] else "non",
            "mx": {1: "ok", 0: "invalide"}.get(ok, "non vérifié"),
            "entreprise": field("name"), "categorie": field("category"), "adresse": field("address"),
            "code_postal": field("postal_code"), "ville": field("city"), "telephone": field("phone"),
            "telephone_site": " / ".join(safe_site_phones[:3]),
            "site_web": field("website") or (e["source_url"] if not b else ""),
            "siren": field("siren"), "naf": field("naf"), "source": field("source"),
            "page_source": e["source_url"] or "", "siret": field("siret"),
            "etat_rattachement": attribution, "preuve_rattachement": evidence,
            "type_source_contact": e["source_type"] or "", "date_collecte": e["found_at"] or "",
            "derniere_verification": checked_at or "", "nombre_etablissements": len(associations[e["email"]]),
            "_association_key": ("business", b["siret"] or b["id"]) if b else ("site", key or e["source_url"] or ""),
        })
    rows.sort(key=lambda r: (_ATTR_RANK.get(r["etat_rattachement"], 9),
                             _KIND_RANK.get(r["type_email"], 9), r["entreprise"], r["page_source"]))
    seen, output = set(), []
    for row in rows:
        key = row["email"] if dedupe else (row["email"], row["_association_key"])
        if key not in seen:
            seen.add(key)
            row.pop("_association_key")
            output.append(row)
    output.sort(key=lambda r: (r["categorie"], r["code_postal"], r["entreprise"], r["email"]))
    _write(path, output, COLUMNS, sep)
    log(f"[export] {len(output)} lignes -> {path}")
    return len(output)


PHONE_COLUMNS = [
    "entreprise", "categorie", "adresse", "code_postal", "ville", "telephone", "telephone_site",
    "emails", "site_web", "siren", "naf", "source", "siret",
]


def export_phones_csv(db: DB, path: str, *, categories: list[str] | None = None, sep: str = ";") -> int:
    """Les téléphones et e-mails d'un réseau ne sont jamais distribués à toutes ses fiches."""
    contacts, unique, site_phones = _associated_contacts(db, include_phone_sites=True)
    biz_emails = defaultdict(set)
    for e, b, _, _ in contacts:
        if b:
            biz_emails[b["id"]].add(e["email"])
    unique_keys = defaultdict(list)
    for key, b in unique.items():
        unique_keys[b["id"]].append(key)
    rows = []
    for b in db.conn.execute("SELECT * FROM businesses"):
        extra = sorted({p for key in unique_keys.get(b["id"], ()) for p in site_phones.get(key, [])})
        if not (b["phone"] or extra) or (categories and b["category"] not in categories):
            continue
        rows.append({
            "entreprise": b["name"] or "", "categorie": b["category"] or "", "adresse": b["address"] or "",
            "code_postal": b["postal_code"] or "", "ville": b["city"] or "", "telephone": b["phone"] or "",
            "telephone_site": " / ".join(extra[:3]), "emails": " / ".join(sorted(biz_emails[b["id"]])),
            "site_web": b["website"] or "", "siren": b["siren"] or "", "naf": b["naf"] or "",
            "source": b["source"] or "", "siret": b["siret"] or "",
        })
    rows.sort(key=lambda r: (r["categorie"], r["code_postal"], r["entreprise"]))
    _write(path, rows, PHONE_COLUMNS, sep)
    log(f"[export] {len(rows)} entreprises avec téléphone -> {path}")
    return len(rows)


def _write(path, rows, columns, sep):
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, delimiter=sep)
        writer.writeheader()
        writer.writerows(rows)
