"""Identités locales et contacts explicitement publiés ensemble.

Les coordonnées d'un objet JSON-LD ou d'une vCard restent sur cet objet. Les
scripts applicatifs (Next, cartes, widgets de voisins) ne sont jamais balayés
pour enrichir arbitrairement l'établissement principal.

Un rapprochement exige un SIRET exact, ou un nom distinctif accompagné d'une
adresse, d'un téléphone ou d'une localité concordante, ou adresse + téléphone.
Le code postal et le SIREN ne suffisent jamais seuls. Les comparaisons de texte
utilisent des blocs visibles distincts : pas de preuve assemblée entre deux
fiches, une barre de navigation et un pied de page.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import unicodedata
from collections.abc import Mapping
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .config import refine_category
from .extract import MAX_SCAN, extract_emails, extract_phones

_AUTO_TYPES = {
    "AutomotiveBusiness", "AutoRepair", "AutoBodyShop", "AutoDealer",
    "AutoPartsStore", "AutoRental", "AutoWash", "GasStation",
    "MotorcycleDealer", "MotorcycleRepair", "TireShop", "DrivingSchool",
    "VehicleInspection", "VehicleInspectionService",
}
_CATEGORY = {
    "AutoRepair": "garage", "AutoBodyShop": "carrosserie",
    "AutoDealer": "concession", "AutoPartsStore": "pieces",
    "AutoRental": "location", "AutoWash": "lavage",
    "GasStation": "station_service", "MotorcycleDealer": "moto",
    "MotorcycleRepair": "moto", "TireShop": "pneus",
    "DrivingSchool": "auto_ecole", "VehicleInspection": "controle_technique",
    "VehicleInspectionService": "controle_technique",
}
_AUTO_NAME = re.compile(
    r"garage|carross|automobil|\bauto\b|\bmoto|pneu|pare.brise|"
    r"contr[oô]le technique|remorqu|depann|dépann", re.I)
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_HIDDEN = {"script", "style", "template", "noscript"}
_SCOPES = {"html", "article", "section", "li", "address", "footer", "header", "aside", "nav", "main", "form"}
_BLOCKS = _SCOPES | {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "dt", "dd"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SPACE = re.compile(r"\s+")
_POSTAL = re.compile(r"(?<!\d)(\d{5})(?!\d)")


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", html.unescape(str(value or "")))
    value = "".join(c for c in value if not unicodedata.combining(c)).lower()
    return _SPACE.sub(" ", re.sub(r"[^a-z0-9]+", " ", value)).strip()


def _scalar(value) -> str:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value).strip()
    if isinstance(value, list):
        return next((s for v in value if (s := _scalar(v))), "")
    if isinstance(value, dict):
        for key in ("@value", "value", "name", "@id"):
            if key in value:
                return _scalar(value[key])
    return ""


def _url(value, base: str, *, fragment: bool = False) -> str:
    raw = _scalar(value)
    if not raw:
        return ""
    try:
        p = urlsplit(urljoin(base, html.unescape(raw)))
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            return ""
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, p.fragment if fragment else ""))
    except ValueError:
        return ""


def _digits(value, size: int) -> str:
    raw = _scalar(value)
    # Never reinterpret a VAT number, UUID or URL as a French establishment ID.
    if not raw or not re.fullmatch(r"[\d\s.\-]+", raw):
        return ""
    digits = re.sub(r"\D", "", raw)
    return digits if len(digits) == size else ""


def _phone(value) -> str:
    raw = _scalar(value)
    values = extract_phones("tel:" + raw)
    return next(iter(sorted(values)), raw.removeprefix("tel:").strip())


def _phone_digits(value) -> str:
    raw = re.sub(r"[^\d+]", "", _scalar(value).split(";", 1)[0])
    if raw.startswith("+33"):
        raw = "0" + raw[3:]
    elif raw.startswith("0033"):
        raw = "0" + raw[4:]
    return raw if re.fullmatch(r"0[1-9]\d{8}", raw) else ""


def _address(value) -> tuple[str, str, str]:
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, (str, dict))), "")
    if isinstance(value, dict):
        return (_scalar(value.get("streetAddress")), _scalar(value.get("postalCode")),
                _scalar(value.get("addressLocality")))
    raw = _scalar(value)
    match = _POSTAL.search(raw)
    if match:
        city = raw[match.end():].strip(" ,;\n")
        city = re.split(r"[,;\n]", city, maxsplit=1)[0].strip()
        return raw[:match.start()].strip(" ,;\n"), match[1], city
    return raw, "", ""


def _category(entity_type: str, name: str) -> str:
    if entity_type in _CATEGORY:
        return refine_category(_CATEGORY[entity_type], name)
    if _AUTO_NAME.search(name):
        return refine_category("garage", name)
    return ""


class _Document(HTMLParser):
    """Script JSON-LD, liens vCard intégrés et petits blocs de texte visible."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool, int]] = []
        self.buffers: dict[int, list[str]] = {0: []}
        self.regions: dict[int, int] = {0: 0}
        self.serial = 0
        self.scripts: list[str] = []
        self.inline_cards: list[str] = []
        self.script: list[str] | None = None

    def _new(self) -> int:
        self.serial += 1
        self.buffers[self.serial] = []
        self.regions[self.serial] = self.serial
        return self.serial

    def _state(self) -> tuple[bool, int]:
        return (self.stack[-1][1], self.stack[-1][2]) if self.stack else (False, 0)

    def _append(self, value: str, scope: int):
        self.buffers[self.regions.get(scope, scope)].append(value)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden, scope = self._state()
        hidden = hidden or tag in _HIDDEN or "hidden" in attrs or attrs.get("aria-hidden") == "true"
        hidden = hidden or bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", attrs.get("style") or "", re.I))
        if tag == "script" and (attrs.get("type") or "").split(";", 1)[0].strip().lower() == "application/ld+json":
            self.script = []
        if not hidden:
            marker = " ".join(attrs.get(k) or "" for k in ("class", "data-testid", "role", "itemtype"))
            if tag in _SCOPES or re.search(r"(?:garage|workshop|store|dealer)[-_ ]card|schema.org/(?:LocalBusiness|AutoRepair)", marker, re.I):
                scope = self._new()
            elif tag in _HEADINGS:
                self.regions[scope] = self._new()
            if tag in _BLOCKS:
                self._append("\n", scope)
        href = attrs.get("href") or ""
        if re.match(r"data:text/(?:x-)?vcard(?:[;,])", href, re.I):
            self.inline_cards.append(href[:MAX_SCAN])
        if tag not in _VOID:
            self.stack.append((tag, bool(hidden), scope))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.scripts.append("".join(self.script))
            self.script = None
        hidden, scope = self._state()
        if not hidden and tag in _BLOCKS:
            self._append("\n", scope)
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        hidden, scope = self._state()
        if not hidden:
            self._append(data, scope)

    @property
    def blocks(self) -> list[str]:
        return [text for parts in self.buffers.values() if (text := "".join(parts).strip())]


def _walk(value):
    # Iterative traversal avoids recursion errors on untrusted, deeply nested JSON.
    queue = [value]
    count = 0
    while queue and count < 20_000:
        item = queue.pop()
        count += 1
        if isinstance(item, dict):
            yield item
            queue.extend(v for v in item.values() if isinstance(v, (dict, list)))
        elif isinstance(item, list):
            queue.extend(v for v in item if isinstance(v, (dict, list)))


def _resolve(value, index: dict):
    if isinstance(value, list):
        return [_resolve(item, index) for item in value]
    if isinstance(value, dict) and isinstance(value.get("@id"), str) and value["@id"] in index:
        # Inline fields take precedence; only explicit @id links are resolved.
        return {**index[value["@id"]], **value}
    return value


def _identifiers(node: dict) -> tuple[str, str]:
    siret, siren = _digits(node.get("siret") or node.get("SIRET"), 14), _digits(node.get("siren") or node.get("SIREN"), 9)
    values = node.get("identifier", [])
    if not isinstance(values, list):
        values = [values]
    for value in values:
        if isinstance(value, dict):
            label = _norm(value.get("propertyID") or value.get("name") or "")
            raw = value.get("value")
            if "siret" in label:
                siret = siret or _digits(raw, 14)
            elif "siren" in label:
                siren = siren or _digits(raw, 9)
        else:
            raw = _scalar(value)
            labelled = re.fullmatch(r"\s*(SIRET|SIREN)\s*[:#]?\s*([\d\s.-]+)\s*", raw, re.I)
            if labelled:
                raw = labelled[2]
            siret = siret or _digits(raw, 14)
            siren = siren or _digits(raw, 9)
    return siret, siren or (siret[:9] if siret else "")


def _fingerprint(entity: dict) -> str:
    identity = [_norm(entity.get(k, "")) for k in ("name", "address", "postal_code", "city", "siret", "siren")]
    return hashlib.sha256("|".join(identity).encode()).hexdigest()[:20]


def _json_entity(node: dict, index: dict, page_url: str) -> dict | None:
    raw_types = node.get("@type", [])
    if not isinstance(raw_types, list):
        raw_types = [raw_types]
    types = [_scalar(t).rsplit("/", 1)[-1].rsplit("#", 1)[-1].rsplit(":", 1)[-1] for t in raw_types]
    entity_type = next((t for t in types if t in _CATEGORY), "")
    entity_type = entity_type or next((t for t in types if t in _AUTO_TYPES), "")
    entity_type = entity_type or ("LocalBusiness" if "LocalBusiness" in types else "")
    if not entity_type:
        return None  # Organization, Person, BreadcrumbList, etc. are not local garages.
    address, postal, city = _address(_resolve(node.get("address"), index))
    siret, siren = _identifiers(node)
    name = _scalar(node.get("name") or node.get("legalName"))
    if not (name or siret):
        return None
    contacts = node.get("contactPoint") or []
    if not isinstance(contacts, list):
        contacts = [contacts]
    contacts = [_resolve(c, index) for c in contacts if isinstance(c, dict)]
    emails = set()
    for value in [node.get("email")] + [c.get("email") for c in contacts]:
        for email in value if isinstance(value, list) else [value]:
            emails.update(extract_emails(_scalar(email)))
    explicit_id = _scalar(node.get("@id"))
    website = _url(node.get("url") or node.get("mainEntityOfPage"), page_url)
    if not website and explicit_id.startswith(("https://", "http://", "/", "./", "../")):
        website = _url(explicit_id, page_url)
    category = _category(entity_type, name)
    # AutomotiveBusiness is broader than a repair garage. The public Top Garage
    # establishment URL supplies that missing context even for short names (2JC).
    try:
        profile = urlsplit(website or page_url)
    except ValueError:
        profile = urlsplit("")
    if (not category and entity_type == "AutomotiveBusiness"
            and profile.hostname == "garage.top-garage.fr"
            and profile.path.rstrip("/").endswith("/details")):
        category = "garage"
    entity = {
        "name": name, "address": address, "postal_code": postal, "city": city,
        "phone": _phone(node.get("telephone") or next((c.get("telephone") for c in contacts if c.get("telephone")), "")),
        "siret": siret, "siren": siren, "website": website,
        "source_url": page_url, "source_type": "jsonld", "entity_type": entity_type,
        "emails": sorted(emails), "category": category,
    }
    if explicit_id:
        entity["source_id"] = _url(explicit_id, page_url, fragment=True) or explicit_id
    elif siret:
        entity["source_id"] = "siret:" + siret
    else:
        # A network homepage may be shared by distinct objects: URL alone is unsafe.
        entity["source_id"] = (website or page_url).split("#", 1)[0] + "#entity-" + _fingerprint(entity)
    return entity


def _split_escaped(value: str, separator: str = ";") -> list[str]:
    parts, current, escaped = [], [], False
    for char in value:
        if char == separator and not escaped:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        escaped = char == "\\" and not escaped
    return parts + ["".join(current)]


def _v_unescape(value: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1].lower() == "n" else m[1], value).strip()


def _v_property(line: str) -> tuple[str, str]:
    quoted = False
    for i, char in enumerate(line):
        if char == '"' and (i == 0 or line[i - 1] != "\\"):
            quoted = not quoted
        elif char == ":" and not quoted:
            return line[:i].split(";", 1)[0].rsplit(".", 1)[-1].upper(), line[i + 1:]
    return "", ""


def _vcards(raw: str, page_url: str) -> list[dict]:
    lines: list[str] = []
    for line in raw.lstrip("\ufeff").splitlines():
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    cards, fields = [], None
    for line in lines:
        key, value = _v_property(line)
        if key == "BEGIN" and value.upper() == "VCARD":
            fields = {}
        elif key == "END" and value.upper() == "VCARD" and fields is not None:
            org = _split_escaped((fields.get("ORG") or [""])[0])[0]
            name = _v_unescape(org or (fields.get("FN") or [""])[0])
            adr = [_v_unescape(v) for v in _split_escaped((fields.get("ADR") or [""])[0])]
            adr += [""] * max(0, 7 - len(adr))
            emails = set()
            for email in fields.get("EMAIL", []):
                emails.update(extract_emails(_v_unescape(email)))
            if name:
                card = {
                    "name": name, "address": ", ".join(v for v in (adr[2], adr[1], adr[0]) if v),
                    "postal_code": adr[5], "city": adr[3],
                    "phone": _phone((fields.get("TEL") or [""])[0]),
                    "siret": _digits((fields.get("X-SIRET") or [""])[0], 14),
                    "siren": _digits((fields.get("X-SIREN") or [""])[0], 9),
                    "website": _url((fields.get("URL") or [""])[0], page_url),
                    "source_url": page_url, "source_type": "vcard", "entity_type": "LocalBusiness",
                    "emails": sorted(emails), "category": _category("LocalBusiness", name),
                }
                card["siren"] = card["siren"] or card["siret"][:9]
                card["source_id"] = _v_unescape((fields.get("UID") or [""])[0]) or page_url.split("#", 1)[0] + "#vcard-" + _fingerprint(card)
                cards.append(card)
            fields = None
        elif fields is not None and key:
            fields.setdefault(key, []).append(value)
    return cards


class _Motrio(HTMLParser):
    """Adaptateur du contact SSR public, limité aux attributs de sa fiche locale."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.location = self.name_depth = self.content_depth = 0
        self.name: list[str] = []
        self.detail: dict | None = None
        self.details: list[dict] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        depth = len(self.stack) + 1
        if tag in _HIDDEN and not self.hidden_depth:
            self.hidden_depth = depth
        marker = attrs.get("data-testid", "")
        if marker == "motrio-workshop-hero-section-title" and tag == "h1":
            self.name_depth = depth
        elif marker == "motrio-workshop-location-section":
            self.location = depth
        elif self.location and marker == "motrio-workshop-location-detail":
            self.detail = {"depth": depth, "icon": "", "content": []}
        elif self.detail is not None and marker == "motrio-workshop-location-detail-content":
            self.content_depth = depth
        if tag == "use" and self.detail is not None:
            self.detail["icon"] = (attrs.get("href") or attrs.get("xlink:href") or "").rsplit("#", 1)[-1]
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not self.stack or tag not in self.stack:
            return
        depth = len(self.stack) - self.stack[::-1].index(tag)
        if self.name_depth >= depth:
            self.name_depth = 0
        if self.content_depth >= depth:
            self.content_depth = 0
        if self.detail is not None and self.detail["depth"] >= depth:
            self.details.append(self.detail)
            self.detail = None
        if self.location >= depth:
            self.location = 0
        if self.hidden_depth >= depth:
            self.hidden_depth = 0
        del self.stack[depth - 1:]

    def handle_data(self, data):
        if self.hidden_depth:
            return
        if self.name_depth:
            self.name.append(data)
        if self.content_depth and self.detail is not None:
            self.detail["content"].append(data)


def _motrio_entity(page_text: str, page_url: str) -> list[dict]:
    try:
        parsed = urlsplit(page_url)
    except ValueError:
        return []
    if (parsed.hostname or "").lower() not in {"motrio.fr", "www.motrio.fr"} or not re.fullmatch(r"/garage-reparateur/[^/]+/?", parsed.path):
        return []
    parser = _Motrio()
    parser.feed(page_text)
    values = {}
    for detail in parser.details:
        values.setdefault(detail["icon"], _SPACE.sub(" ", "".join(detail["content"])).strip())
    name = _SPACE.sub(" ", "".join(parser.name)).strip()
    address, postal, city = _address(values.get("svg-travel/marker-pin-01", ""))
    if not name or not (address and postal):
        return []  # A changed DOM must not produce an unscoped contact.
    return [{
        "name": name, "address": address, "postal_code": postal, "city": city,
        "phone": _phone(values.get("svg-communication/phone", "")),
        "siret": "", "siren": "", "website": _url(page_url, page_url),
        "source_url": page_url, "source_type": "motrio_public", "source_id": _url(page_url, page_url),
        "entity_type": "AutoRepair", "category": refine_category("garage", name),
        "emails": sorted(extract_emails(values.get("svg-communication/mail-01", ""))),
    }]


def extract_entities(page_text: str, page_url: str) -> list[dict]:
    """Renvoie des établissements séparés, avec leurs propres contacts et provenance."""
    page_text = page_text[:MAX_SCAN]
    if page_text.lstrip("\ufeff \r\n\t").upper().startswith("BEGIN:VCARD"):
        return _vcards(page_text, page_url)
    document = _Document()
    document.feed(page_text)
    nodes = []
    for script in document.scripts:
        script = script.strip().removeprefix("<!--").removesuffix("-->").strip()
        try:
            parsed = json.loads(script)
        except (ValueError, RecursionError):
            try:
                parsed = json.loads(html.unescape(script))
            except (ValueError, RecursionError):
                continue
        nodes.extend(_walk(parsed))
    index = {n["@id"]: n for n in nodes if isinstance(n.get("@id"), str) and len(n) > 1}
    entities = [entity for node in nodes if (entity := _json_entity(node, index, page_url))]
    for uri in document.inline_cards:
        header, _, payload = uri.partition(",")
        try:
            raw = base64.b64decode(payload, validate=True).decode("utf-8", "replace") if ";base64" in header.lower() else unquote(payload)
        except (ValueError, UnicodeError):
            continue
        entities.extend(_vcards(raw, page_url))
    entities.extend(_motrio_entity(page_text, page_url))
    unique = {}
    identifiers: dict[tuple[str, str], set[str]] = {}
    for entity in entities:
        identifiers.setdefault((entity["source_type"], entity["source_id"]), set()).add(_fingerprint(entity))
    for entity in entities:
        # Even a reused/malformed @id must not merge different locations.
        if len(identifiers[(entity["source_type"], entity["source_id"])]) > 1:
            entity["source_id"] += "|identity:" + _fingerprint(entity)
        key = (entity["source_type"], entity["source_id"], _fingerprint(entity))
        if key in unique:
            unique[key]["emails"] = sorted(set(unique[key]["emails"]) | set(entity["emails"]))
        else:
            unique[key] = entity
    return list(unique.values())


_NAME_GENERIC = set("garage garages auto automobile automobiles carrosserie sas sasu sarl sa eurl ets societe de du des le la les l et au aux service services centre atelier reparation reparations".split())
_STREET_ALIASES = {"av": "avenue", "bd": "boulevard", "boul": "boulevard", "rte": "route", "ch": "chemin", "imp": "impasse", "pl": "place", "st": "saint", "ste": "sainte"}
_ADDRESS_LINE = re.compile(r"\b\d{1,4}(?:\s+(?:bis|ter))?\s+(?:rue|avenue|av\.?|boulevard|bd|route|rte|chemin|impasse|place|allee|quai|cours|passage|square)\b[^\n;]{2,160}", re.I)


def _name_matches(business: dict, value: str, *, allow_short: bool = False) -> bool:
    actual = set(_norm(value).split())
    for key in ("name", "alt_name"):
        words = set(_norm(business.get(key, "")).split()) - _NAME_GENERIC
        if words and any(len(w) >= 3 for w in words) and words <= actual:
            return True
        if allow_short and words and words == actual - _NAME_GENERIC:
            return True
    return False


def _street(raw: str, city: str = "") -> str:
    raw = _POSTAL.split(str(raw or ""), maxsplit=1)[0]
    words = [_STREET_ALIASES.get(w, w) for w in _norm(raw).split()]
    city_words = _norm(city).split()
    if city_words and words[-len(city_words):] == city_words:
        words = words[:-len(city_words)]
    return " ".join(words).strip()


def _street_agrees(a: str, b: str) -> bool:
    if len(a.split()) < 2 or len(b.split()) < 2:
        return False
    return f" {a} " in f" {b} " or f" {b} " in f" {a} "


def _business_siret(business: dict) -> str:
    return _digits(business.get("siret"), 14) or (_digits(business.get("source_id"), 14) if business.get("source") == "sirene" else "")


def _success(name: bool, address: bool, phone: bool, locality: bool) -> tuple[bool, str]:
    if name and (address or phone or locality):
        evidence = [label for label, yes in (("nom", name), ("adresse", address), ("telephone", phone), ("localite", locality)) if yes]
        return True, "+".join(evidence)
    if address and phone:
        return True, "adresse+telephone"
    return False, "preuves_insuffisantes"


def _match_entity(business: dict, entity: dict) -> tuple[bool, str]:
    expected = _business_siret(business)
    actual = _digits(entity.get("siret"), 14)
    siren = _digits(business.get("siren"), 9) or expected[:9]
    actual_siren = _digits(entity.get("siren"), 9) or actual[:9]
    if expected and actual and expected != actual:
        return False, "conflit_siret"
    if siren and actual_siren and siren != actual_siren:
        return False, "conflit_siren"
    cp, actual_cp = _scalar(business.get("postal_code")), _scalar(entity.get("postal_code"))
    if cp and actual_cp and cp != actual_cp:
        return False, "conflit_code_postal"
    address = _street(business.get("address", ""), business.get("city", ""))
    actual_address = _street(entity.get("address", ""), entity.get("city", ""))
    same_address = _street_agrees(address, actual_address)
    if len(address.split()) >= 2 and len(actual_address.split()) >= 2 and not same_address:
        return False, "conflit_adresse"
    if expected and actual == expected:
        return True, "siret_exact"
    phone = _phone_digits(business.get("phone"))
    same_phone = bool(phone and phone == _phone_digits(entity.get("phone")))
    city, actual_city = _norm(business.get("city", "")), _norm(entity.get("city", ""))
    locality = bool(cp and cp == actual_cp) if cp else bool(city and city == actual_city)
    return _success(_name_matches(business, entity.get("name", ""), allow_short=same_address or same_phone),
                    same_address, same_phone, locality)


def _labelled_ids(text: str, label: str, size: int) -> set[str]:
    pattern = rf"\b{label}\b\s*(?:n[°ºo]?\s*)?[:#\-]?\s*((?:\d[\s.\-]*){{{size - 1}}}\d)(?!\d)"
    return {re.sub(r"\D", "", m[1]) for m in re.finditer(pattern, text, re.I)}


def match_identity(business: Mapping, text: str = "", entity: dict | None = None) -> tuple[bool, str]:
    """Rapprochement prudent ; retourne aussi un motif court et exportable.

    Si ``entity`` est fourni, seul cet objet est comparé : le texte global ne
    peut jamais compléter les coordonnées d'un voisin. Sans objet, les preuves
    positives doivent coexister dans un bloc visible. Les contradictions des
    identifiants explicites restent bloquantes, même si un autre bloc concorde.
    """
    business = dict(business)  # Accepte également sqlite3.Row.
    if entity is not None:
        return _match_entity(business, entity)
    document = _Document()
    document.feed(text[:MAX_SCAN])
    blocks = document.blocks
    visible = "\n".join(blocks)
    expected = _business_siret(business)
    siren = _digits(business.get("siren"), 9) or expected[:9]
    sirets = _labelled_ids(visible, "siret", 14)
    sirens = _labelled_ids(visible, "siren", 9)
    if expected and sirets and sirets != {expected}:
        return False, "conflit_siret" if expected not in sirets else "plusieurs_siret"
    if siren and (sirens or sirets) and any(value != siren for value in sirens | {s[:9] for s in sirets}):
        return False, "conflit_siren"
    address = _street(business.get("address", ""), business.get("city", ""))
    candidates = {_street(m[0]) for block in blocks for m in _ADDRESS_LINE.finditer(unicodedata.normalize("NFKD", block))}
    if len(address.split()) >= 2 and len(candidates) == 1 and not _street_agrees(address, next(iter(candidates))):
        return False, "conflit_adresse"
    if expected and expected in sirets:
        return True, "siret_exact"
    phone = _phone_digits(business.get("phone"))
    cp, city = _scalar(business.get("postal_code")), _norm(business.get("city", ""))
    for block in blocks:
        # A long unstructured document does not make distant facts one contact block.
        for offset in range(0, len(block), 1000):
            part = block[max(0, offset - 200):offset + 1000]
            normalized = _norm(part)
            same_address = bool(address and len(address.split()) >= 2 and f" {address} " in f" {_street(part)} ")
            same_phone = bool(phone and phone in {_phone_digits(p) for p in extract_phones(part)})
            locality = bool(cp and re.search(rf"(?<!\d){re.escape(cp)}(?!\d)", part)) if cp else bool(city and f" {city} " in f" {normalized} ")
            matched = _success(_name_matches(business, part), same_address, same_phone, locality)
            if matched[0]:
                return matched
    return False, "preuves_insuffisantes"
