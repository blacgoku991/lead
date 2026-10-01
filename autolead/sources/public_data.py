"""Deux sources publiques structurées, sans import de colonnes d'adresses e-mail.

DGCCRF fournit l'identité et les coordonnées des centres de contrôle technique.
DINUM fournit des correspondances SIRET -> domaine de messagerie : ces domaines
restent des candidats à vérifier, jamais des sites officiels présumés.

Les téléchargements sont bornés et mis sur disque avant lecture. Le CSV DINUM
est lu par petits lots, sans charger le fichier ni tous les SIRET en mémoire.
Un échec HTTP/de format lève PublicDataError ; il n'est pas annoncé comme un
import réussi de zéro ligne. Les écritures par lots permettent une reprise
idempotente si une erreur de CSV apparaît après un premier lot validé.
"""
from __future__ import annotations

import asyncio
import csv
import io
import ipaddress
import json
import math
import re
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable, TextIO
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from ..config import BAD_EMAIL_DOMAINS, BAD_EMAIL_TLDS, BLOCKED_SITE_DOMAINS, FREE_EMAIL_DOMAINS, PLACEHOLDER_DOMAIN_RE
from ..db import DB
from ..utils import SHARED_HOSTS, domain_in, log

DGCCRF_URL = (
    "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/"
    "annuaire-centres-controle-technique/exports/json"
)
DGCCRF_DATASET_URL = "https://www.data.gouv.fr/datasets/annuaire-des-centres-de-controle-technique"
DINUM_URL = "https://www.data.gouv.fr/api/1/datasets/r/4208f064-e655-4bad-93c9-9a3977f3f8cc"
DINUM_DATASET_URL = "https://www.data.gouv.fr/datasets/domaines-email-de-contact-par-organisation-francaise"
# Précision de la version connue, et non date supposée de chaque association.
DINUM_SOURCE_DATE = "2024-09"
PUBLIC_LICENSE = "etalab-2.0"
MAX_DGCCRF_BYTES = 32 * 1024 * 1024
MAX_DINUM_BYTES = 128 * 1024 * 1024
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=300, connect=20, sock_read=30)
CHUNK_SIZE = 128 * 1024
BATCH_SIZE = 200  # <= 400 variables SQLite pour la jointure avec fallback SIRENE

_SIRET = re.compile(r"[0-9]{14}\Z")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_NON_PUBLIC_SUFFIXES = {"localhost", "local", "localdomain", "internal", "lan", "invalid", "test", "example", "onion"}
# Compléments aux fournisseurs déjà présents dans la configuration de l'outil.
_MAIL_PROVIDERS = FREE_EMAIL_DOMAINS | {
    "protonmail.ch", "proton.me", "pm.me", "tuta.com", "tutamail.com", "tuta.io",
    "tutanota.de", "yandex.com", "yandex.fr", "mail.ru", "inbox.ru", "list.ru",
    "bk.ru", "qq.com", "163.com", "126.com", "gmx.net", "gmx.de", "gmx.com",
}


class PublicDataError(RuntimeError):
    """Source inaccessible, trop volumineuse ou incompatible avec le schéma attendu."""


@dataclass(frozen=True)
class _Snapshot:
    stream: TextIO
    source_url: str
    source_date: str
    source_license: str


def _validate_limit(limit: int | None) -> None:
    if limit is not None and (not isinstance(limit, int) or limit < 0):
        raise PublicDataError("La limite de lignes doit être un entier positif ou nul.")


def _text(value) -> str:
    return str(value).strip() if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _siret(value) -> str | None:
    value = _text(value)
    return value if _SIRET.fullmatch(value) else None


def _hostname(value: str) -> str | None:
    """Valide un nom DNS nu (sans URL, port, identifiant ou adresse IP)."""
    value = value.strip().lower().removesuffix(".")
    if not value or any(c in value for c in "/:@?#\\") or any(c.isspace() for c in value):
        return None
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if len(value) > 253:
        return None
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        return None
    labels = value.split(".")
    if len(labels) < 2 or not all(_DNS_LABEL.fullmatch(label) for label in labels):
        return None
    tld = labels[-1]
    if not (len(tld) >= 2 and (tld.isalpha() or tld.startswith("xn--"))):
        return None
    if tld in _NON_PUBLIC_SUFFIXES or tld in BAD_EMAIL_TLDS:
        return None
    return value


def normalize_candidate_domain(value) -> str | None:
    """Accepte seulement un domaine d'entreprise plausible, sans fabriquer d'e-mail."""
    if not isinstance(value, str):
        return None
    host = _hostname(value)
    if not host or domain_in(host, _MAIL_PROVIDERS | BAD_EMAIL_DOMAINS) or PLACEHOLDER_DOMAIN_RE.fullmatch(host):
        return None
    return host


def _website(value) -> str:
    """Garde l'URL complète d'une fiche, y compris ses paramètres identifiants."""
    if not isinstance(value, str) or not value.strip():
        return ""
    raw = value.strip()
    if any(c.isspace() for c in raw):
        return ""
    if "://" not in raw:
        raw = "https://" + raw.lstrip("/")
    try:
        parsed = urlsplit(raw)
        host = _hostname(parsed.hostname or "")
        port = parsed.port  # détecte aussi un port syntaxiquement invalide
    except (ValueError, UnicodeError):
        return ""
    if parsed.scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None:
        return ""
    if host not in SHARED_HOSTS and domain_in(host, BLOCKED_SITE_DOMAINS | _MAIL_PROVIDERS):
        return ""
    if host in SHARED_HOSTS and not parsed.path.strip("/"):
        return ""
    netloc = host + (f":{port}" if port is not None else "")
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))


def _header_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""


@asynccontextmanager
async def _open_snapshot(session, *, name: str, url: str, path, max_bytes: int,
                         source_date: str | None, source_license: str | None):
    """Télécharge/copie un snapshot borné ; les fichiers temporaires sont supprimés."""
    official = path is None and url in {DGCCRF_URL, DINUM_URL}
    provenance = str(Path(path).expanduser().resolve()) if path is not None else url
    # Sur la ressource DINUM connue, un Last-Modified HTTP récent peut refléter
    # un déplacement de fichier (le snapshot publié reste celui de septembre
    # 2024). Il ne doit pas rajeunir artificiellement la version des données.
    date = source_date or (DINUM_SOURCE_DATE if official and url == DINUM_URL else "")
    license_name = source_license if source_license is not None else (PUBLIC_LICENSE if official else "")
    size = 0
    with tempfile.TemporaryFile(mode="w+b") as raw:
        try:
            if path is not None:
                local_path = Path(path).expanduser()
                if not local_path.is_file():
                    raise PublicDataError(f"[{name}] snapshot local introuvable ou non régulier : {local_path}")
                if local_path.stat().st_size > max_bytes:
                    raise PublicDataError(f"[{name}] snapshot supérieur à la limite de {max_bytes // (1024 * 1024)} Mio")
                with local_path.open("rb") as local:
                    while chunk := local.read(CHUNK_SIZE):
                        size += len(chunk)
                        if size > max_bytes:
                            raise PublicDataError(f"[{name}] snapshot supérieur à la limite de taille")
                        raw.write(chunk)
                        if size % (16 * CHUNK_SIZE) == 0:
                            await asyncio.sleep(0)
            else:
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
                    raise PublicDataError(f"[{name}] une URL HTTP(S) sans identifiants est requise")
                async with session.get(url, timeout=DOWNLOAD_TIMEOUT,
                                       headers={"Accept": "application/json,text/csv,text/plain;q=0.9"}) as response:
                    if response.status != 200:
                        raise PublicDataError(f"[{name}] téléchargement refusé : HTTP {response.status} ({url})")
                    if response.content_length is not None and response.content_length > max_bytes:
                        raise PublicDataError(f"[{name}] réponse supérieure à la limite de {max_bytes // (1024 * 1024)} Mio")
                    date = date or _header_date(response.headers.get("Last-Modified"))
                    async for chunk in response.content.iter_chunked(CHUNK_SIZE):
                        size += len(chunk)
                        if size > max_bytes:
                            raise PublicDataError(f"[{name}] réponse supérieure à la limite de taille")
                        raw.write(chunk)
        except PublicDataError:
            raise
        except asyncio.TimeoutError as exc:
            raise PublicDataError(f"[{name}] délai de téléchargement dépassé ; aucun succès déclaré") from exc
        except (aiohttp.ClientError, OSError, ValueError) as exc:
            raise PublicDataError(f"[{name}] snapshot inaccessible : {type(exc).__name__}: {exc}") from exc
        if not size:
            raise PublicDataError(f"[{name}] source vide : aucune donnée importée")
        if not date and official and url == DINUM_URL:
            date = DINUM_SOURCE_DATE
        log(f"[{name}] snapshot prêt : {size / (1024 * 1024):.2f} Mio"
            + (f", version/date source {date}" if date else ", date source inconnue"))
        raw.seek(0)
        with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text:
            yield _Snapshot(text, provenance, date, license_name)


def _csv_rows(stream: TextIO, required: set[str], name: str) -> Iterable[dict]:
    # Lire seulement l'en-tête pour choisir le séparateur évite les erreurs du
    # Sniffer sur des champs contenant une virgule ou un retour à la ligne.
    for sep in (",", ";", "\t", "|"):
        stream.seek(0)
        reader = csv.reader(stream, delimiter=sep, strict=True)
        try:
            header = next(reader)
        except StopIteration:
            raise PublicDataError(f"[{name}] CSV sans en-tête")
        except csv.Error:
            continue
        header = [column.strip().lower() for column in header]
        if required.issubset(header):
            if len(header) != len(set(header)):
                raise PublicDataError(f"[{name}] CSV avec colonnes dupliquées")
            break
    else:
        raise PublicDataError(f"[{name}] colonnes CSV attendues : {', '.join(sorted(required))}")
    for row in reader:
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(row) != len(header):
            raise PublicDataError(f"[{name}] nombre de colonnes incorrect à la ligne {reader.line_num}")
        yield dict(zip(header, row))


def _dgccrf_rows(stream: TextIO) -> Iterable[dict]:
    start = stream.read(4096).lstrip()
    stream.seek(0)
    if not start.startswith(("[", "{")):
        yield from _csv_rows(stream, {"cct_siret", "cct_denomination"}, "dgccrf")
        return
    payload = json.load(stream)
    if isinstance(payload, dict):
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise PublicDataError("[dgccrf] JSON attendu : liste d'établissements ou objet results")
        total = payload.get("total_count", len(rows))
        if not isinstance(total, int) or total != len(rows):
            raise PublicDataError("[dgccrf] réponse paginée incomplète : utiliser l'URL d'export JSON complet")
    elif isinstance(payload, list):
        rows = payload
    else:
        raise PublicDataError("[dgccrf] le JSON doit contenir une liste d'établissements")
    if rows and not any(isinstance(row, dict) and {"cct_siret", "cct_denomination"}.issubset(row) for row in rows):
        raise PublicDataError("[dgccrf] schéma incompatible : cct_siret et cct_denomination absents")
    yield from rows


def _postcode(value) -> str:
    code = _text(value)
    if code.isascii() and code.isdigit() and len(code) in {4, 5}:
        return code.zfill(5)
    return ""


def _department(postcode: str) -> str:
    if len(postcode) != 5:
        return ""
    if postcode.startswith(("97", "98")):
        return postcode[:3]
    if postcode.startswith("20"):
        return "2A" if postcode < "20200" else "2B"
    return postcode[:2]


def _record_department(row: dict, postcode: str) -> str:
    explicit = _text(row.get("code_departement")).upper()
    if re.fullmatch(r"[0-9]{1,3}|2[AB]", explicit):
        return explicit.zfill(2)
    commune = _text(row.get("cct_code_commune")).upper()
    if re.fullmatch(r"(?:[0-9]{2}|2[AB])[0-9]{3}", commune):
        return commune[:3] if commune.startswith(("97", "98")) else commune[:2]
    return _department(postcode)


def _coordinate(value, bound: float) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and abs(result) <= bound else None


async def collect_dgccrf(db: DB, session: aiohttp.ClientSession, *, departements=None,
                         categories=None, limit: int | None = None, path=None,
                         url: str = DGCCRF_URL, source_date: str | None = None,
                         source_license: str | None = None) -> int:
    """Importe les centres ; limit borne les lignes examinées AVANT les filtres.

    Retour : nombre de nouveaux établissements. Un rapprochement SIRET peut
    enrichir les champs vides d'un établissement existant sans augmenter ce nombre.
    Les instantanés locaux acceptés sont des exports JSON ou CSV de cette source.
    """
    _validate_limit(limit)
    if categories is not None and "controle_technique" not in categories:
        log("[dgccrf] ignoré : catégorie controle_technique non sélectionnée")
        return 0
    if limit == 0:
        log("[dgccrf] limite 0 : import non lancé")
        return 0
    deps = {str(dep).upper().zfill(2) for dep in departements} if departements else None
    read = added = invalid = filtered = selected = 0
    limited = False
    batch: list[dict] = []
    try:
        async with _open_snapshot(session, name="dgccrf", url=url, path=path,
                                  max_bytes=MAX_DGCCRF_BYTES, source_date=source_date,
                                  source_license=source_license) as snapshot:
            for row in _dgccrf_rows(snapshot.stream):
                if limit is not None and read >= limit:
                    limited = True
                    break
                read += 1
                if not isinstance(row, dict) or not (siret := _siret(row.get("cct_siret"))) or not _text(row.get("cct_denomination")):
                    invalid += 1
                    continue
                postal_code = _postcode(row.get("cct_code_postal"))
                if deps and _record_department(row, postal_code) not in deps:
                    filtered += 1
                    continue
                selected += 1
                batch.append({
                    "source": "dgccrf", "source_id": siret, "siret": siret,
                    "siren": siret[:9], "name": _text(row.get("cct_denomination")),
                    "category": "controle_technique", "naf": "71.20A", "country": "FR",
                    "address": _text(row.get("cct_adresse")), "postal_code": postal_code,
                    "city": _text(row.get("cct_commune")), "phone": _text(row.get("cct_tel")),
                    "website": _website(row.get("cct_url")),
                    "lat": _coordinate(row.get("latitude", row.get("lat")), 90),
                    "lon": _coordinate(row.get("longitude", row.get("long")), 180),
                    "source_url": snapshot.source_url, "source_date": snapshot.source_date,
                    "source_license": snapshot.source_license,
                })
                if len(batch) >= BATCH_SIZE:
                    added += db.upsert_public_businesses(batch)
                    batch.clear()
                    await asyncio.sleep(0)
            if batch:
                added += db.upsert_public_businesses(batch)
    except PublicDataError as exc:
        if added:
            raise PublicDataError(f"{exc} ; import interrompu, {added} ajouts déjà enregistrés (reprise sans doublons)") from exc
        raise
    except (UnicodeError, csv.Error, json.JSONDecodeError) as exc:
        raise PublicDataError(f"[dgccrf] source mal formée après {read} lignes ; {added} ajouts déjà enregistrés : {exc}") from exc
    log(f"[dgccrf] {read} lignes examinées, {selected} centres retenus, {added} nouveaux établissements, "
        f"{invalid} lignes invalides, {filtered} hors filtre"
        + (" ; LIMITÉ : des lignes restent à traiter" if limited else " ; fin de la source"))
    return added


def _has_siret_column(db: DB) -> bool:
    return any(row[1] == "siret" for row in db.conn.execute("PRAGMA table_info(businesses)"))


def _matching_businesses(db: DB, sirets: set[str], has_siret: bool) -> dict[str, set[int]]:
    matches: dict[str, set[int]] = {}
    if not sirets:
        return matches
    # Borne le nombre de paramètres même si cette fonction est réutilisée ailleurs.
    values = sorted(sirets)
    for start in range(0, len(values), BATCH_SIZE):
        chunk = values[start:start + BATCH_SIZE]
        placeholders = ",".join("?" for _ in chunk)
        if has_siret:
            sql = (
                f"SELECT id, siret FROM businesses WHERE siret IN ({placeholders}) "
                "UNION ALL SELECT id, source_id FROM businesses WHERE source='sirene' "
                f"AND (siret IS NULL OR siret='') AND source_id IN ({placeholders})"
            )
            params = chunk + chunk
        else:
            sql = f"SELECT id, source_id FROM businesses WHERE source='sirene' AND source_id IN ({placeholders})"
            params = chunk
        for business_id, siret in db.conn.execute(sql, params):
            matches.setdefault(siret, set()).add(business_id)
    return matches


def _save_dinum_batch(db: DB, rows: list[dict], snapshot: _Snapshot, counts: dict,
                      has_siret: bool) -> None:
    sirets = {_siret(row.get("siret")) for row in rows}
    owners = _matching_businesses(db, sirets - {None}, has_siret)
    candidates = []
    for row in rows:
        siret = _siret(row.get("siret"))
        if not siret:
            counts["invalid_rows"] += 1
            continue
        if siret not in owners:
            counts["unmatched_rows"] += 1
            continue
        counts["matched_rows"] += 1
        domain = normalize_candidate_domain(row.get("domain_email"))
        if not domain:
            counts["ignored_domains"] += 1
            continue
        for business_id in sorted(owners[siret]):
            candidates.append({
                "business_id": business_id, "domain": domain, "siret": siret,
                "source_url": snapshot.source_url, "source_date": snapshot.source_date,
                "source_license": snapshot.source_license,
                "data_source": _text(row.get("data_source")),
            })
    if candidates:
        counts["candidates"] += db.add_domain_candidates(candidates)


async def enrich_dinum(db: DB, session: aiohttp.ClientSession, *, path=None,
                       url: str = DINUM_URL, limit: int | None = None,
                       source_date: str | None = None, source_license: str | None = None) -> dict:
    """Joint le CSV aux seuls SIRET exacts connus, sans lire de colonne e-mail.

    limit borne les lignes source examinées, avant jointure et filtres. candidates
    compte les nouvelles associations établissement/domaine, pas des sites validés.
    matched_rows inclut les domaines ensuite ignorés (webmail ou syntaxe invalide).
    limited signale un échantillon incomplet ; aucune limite ne signifie pas que
    les domaines ont été visités ni que leur activité a été vérifiée.
    """
    _validate_limit(limit)
    counts = {"rows_read": 0, "matched_rows": 0, "candidates": 0, "invalid_rows": 0,
              "ignored_domains": 0, "unmatched_rows": 0, "limited": False}
    if limit == 0:
        counts["limited"] = True
        log("[dinum] limite 0 : import non lancé")
        return counts
    has_siret = _has_siret_column(db)
    batch: list[dict] = []
    try:
        async with _open_snapshot(session, name="dinum", url=url, path=path,
                                  max_bytes=MAX_DINUM_BYTES, source_date=source_date,
                                  source_license=source_license) as snapshot:
            for row in _csv_rows(snapshot.stream, {"siret", "domain_email", "data_source"}, "dinum"):
                if limit is not None and counts["rows_read"] >= limit:
                    counts["limited"] = True
                    break
                counts["rows_read"] += 1
                batch.append(row)
                if len(batch) >= BATCH_SIZE:
                    _save_dinum_batch(db, batch, snapshot, counts, has_siret)
                    batch.clear()
                    await asyncio.sleep(0)
            if batch:
                _save_dinum_batch(db, batch, snapshot, counts, has_siret)
    except PublicDataError as exc:
        if counts["candidates"]:
            raise PublicDataError(f"{exc} ; import interrompu, {counts['candidates']} candidats déjà enregistrés (reprise sans doublons)") from exc
        raise
    except (UnicodeError, csv.Error) as exc:
        raise PublicDataError(f"[dinum] CSV mal formé après {counts['rows_read']} lignes ; "
                              f"{counts['candidates']} candidats déjà enregistrés : {exc}") from exc
    log(f"[dinum] {counts['rows_read']} lignes examinées, {counts['matched_rows']} avec SIRET connu, "
        f"{counts['candidates']} nouveaux candidats à vérifier, {counts['ignored_domains']} domaines ignorés, "
        f"{counts['invalid_rows']} lignes invalides, {counts['unmatched_rows']} SIRET hors base"
        + (" ; LIMITÉ : des lignes restent à traiter" if counts["limited"] else " ; fin de la source"))
    return counts
