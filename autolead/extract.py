"""Extraction d'e-mails et des liens "contact / mentions légales" depuis du HTML.

Rapide : on ne lance pas de regex coûteuse sur toute la page, on part de chaque "@".
Gère mailto: encodés, entités HTML, \\u0040, protection Cloudflare, "contact [at] x [dot] fr".
"""
from __future__ import annotations

import html
import re
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .config import (
    BAD_EMAIL_DOMAINS,
    BAD_EMAIL_TLDS,
    BAD_LOCAL_PARTS,
    BAD_LOCAL_RE,
    BLOCKED_SITE_DOMAINS,
    FREE_EMAIL_DOMAINS,
    PLACEHOLDER_DOMAIN_RE,
    ROLE_LOCAL_PARTS,
)
from .utils import domain_in, registrable

# Un lien externe n'est suivi que s'il sent l'entreprise automobile (ancre ou contexte)
_AUTO_LINK = re.compile(
    r"garage|carross|automobile|\bauto\b|m[ée]canique|pneu|pare[- ]?brise|concession|"
    r"partenaire|r[ée]seau|nos agences|nos sites|voir le site|site officiel|"
    r"d[ée]pannage|remorquage|contr[ôo]le technique|moto\b|utilitaire|poids lourds",
    re.I,
)

_LOCAL_TAIL = re.compile(r"[a-z0-9._%+-]{1,64}\Z", re.I)
_DOMAIN_HEAD = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}(?![a-z0-9-])", re.I)
_LOCAL_OK = re.compile(r"[a-z0-9._%+-]+")
_DOMAIN_OK = re.compile(r"[a-z0-9.-]+")
_HEX_LOCAL = re.compile(r"[0-9a-f]{20,}")

_UESC = re.compile(r"\\u00([0-9a-fA-F]{2})|\\x([0-9a-fA-F]{2})")
_MAILTO = re.compile(r"mailto:([^\"'<>\s]+)", re.I)
_CF_ATTR = re.compile(r"data-cfemail=[\"']([0-9a-fA-F]+)[\"']")
_CF_HREF = re.compile(r"/cdn-cgi/l/email-protection#([0-9a-fA-F]+)")

_AT = r"(?:\s*[\[\(\{]\s*(?:at|arobase|@)\s*[\]\)\}]\s*|\s+arobase\s+)"
_DOT = r"(?:\s*[\[\(\{]\s*(?:dot|point|\.)\s*[\]\)\}]\s*|\s+point\s+|\.)"
_OBF_HINT = re.compile(r"[\[\(\{]\s*(?:at|arobase|@)\s*[\]\)\}]|\barobase\b", re.I)
_OBF = re.compile(rf"(?<![a-z0-9._%+-])([a-z0-9][a-z0-9._%+-]{{0,63}}){_AT}([a-z0-9-]+(?:{_DOT}[a-z0-9-]+)+)", re.I)
_DOT_SUB = re.compile(r"\s*[\[\(\{]\s*(?:dot|point|\.)\s*[\]\)\}]\s*|\s+point\s+", re.I)
_JS_CONCAT = re.compile(r"""(['"])\s*\+\s*\1""")  # 'contact' + '@' + 'garage.fr'
_DATA_ATTRS = re.compile(
    r"""data-(?:user|name|mail-?user|local)=["']([a-z0-9._%+-]+)["'][^>]{0,200}?"""
    r"""data-(?:domain|host|mail-?domain)=["']([a-z0-9.-]+\.[a-z]{2,24})["']""", re.I)

_A_OPEN = re.compile(r"<a\b([^>]*)>", re.I)
_HREF = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_TAGS = re.compile(r"<[^>]+>")
_SKIP_EXT = re.compile(
    r"\.(?:jpe?g|png|gif|svg|webp|avif|pdf|zip|rar|mp4|mp3|avi|mov|docx?|xlsx?|pptx?|css|js|ico|woff2?|ttf|xml)$",
    re.I,
)

# Pages où l'on trouve des e-mails, avec un score de priorité
CONTACT_HINTS = [
    (re.compile(r"contact|joindre|[ée]crire", re.I), 10),
    (re.compile(r"mentions|l[ée]gal|impressum|imprint", re.I), 9),
    (re.compile(r"coordonn|infos?[- ]pratiques|nous[- ]trouver|plan[- ]d[- ]acc[eè]s|horaires", re.I), 6),
    (re.compile(r"a[- ]propos|à propos|about|qui[- ]sommes|[ée]quipe|team|notre[- ](?:entreprise|soci[ée]t[ée]|garage)", re.I), 5),
    (re.compile(r"devis|rendez|\brdv\b|r[ée]servation", re.I), 4),
    (re.compile(r"\bcgv\b|conditions[- ]g[ée]n[ée]rales", re.I), 2),
]

MAX_SCAN = 2_000_000  # caractères analysés par page


def _cf_decode(hexstr: str) -> str:
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr) - 1, 2))
    except ValueError:
        return ""


def _bad_domain(domain: str) -> bool:
    return bool(PLACEHOLDER_DOMAIN_RE.match(domain)) or domain_in(domain, BAD_EMAIL_DOMAINS)


def clean_email(raw: str) -> str | None:
    """Normalise et valide une adresse ; None si invalide ou factice."""
    e = raw.strip().strip(".,;:'\"<>()[]{}").lower()
    if e.startswith("mailto:"):
        e = e[7:]
    if e.count("@") != 1 or len(e) > 254:
        return None
    local, domain = e.split("@")
    local = local.strip("._%+-")
    domain = domain.strip(".-")
    if not local or not domain or len(local) > 64 or "." not in domain:
        return None
    if ".." in local or ".." in domain:
        return None
    if not _LOCAL_OK.fullmatch(local) or not _DOMAIN_OK.fullmatch(domain):
        return None
    tld = domain.rsplit(".", 1)[1]
    if not tld.isalpha() or len(tld) < 2 or tld in BAD_EMAIL_TLDS:
        return None
    if _bad_domain(domain):
        return None
    if local in BAD_LOCAL_PARTS or BAD_LOCAL_RE.match(local) or _HEX_LOCAL.fullmatch(local):
        return None
    return f"{local}@{domain}"


def extract_emails(text: str) -> set[str]:
    text = text[:MAX_SCAN]
    candidates: list[str] = []

    for h in _CF_ATTR.findall(text) + _CF_HREF.findall(text):
        candidates.append(_cf_decode(h))

    if "\\u00" in text or "\\x" in text:
        text = _UESC.sub(lambda m: chr(int(m.group(1) or m.group(2), 16)), text)
    if "&" in text:
        text = html.unescape(text)

    if "+" in text:
        text = _JS_CONCAT.sub("", text)
    for local, dom in _DATA_ATTRS.findall(text):
        candidates.append(f"{local}@{dom}")

    for m in _MAILTO.findall(text):
        target = unquote(m).split("?", 1)[0]
        candidates.extend(target.split(","))

    # Balayage à partir de chaque "@" (bien plus rapide qu'une regex sur toute la page)
    i = text.find("@")
    while i != -1:
        j = i - 1 if i > 0 and text[i - 1] == " " else i      # tolère "contact @ garage.fr"
        lm = _LOCAL_TAIL.search(text, max(0, j - 64), j)
        if lm:
            dm = _DOMAIN_HEAD.match(text, i + 2 if text[i + 1:i + 2] == " " else i + 1)
            if dm:
                candidates.append(f"{lm.group()}@{dm.group()}")
        i = text.find("@", i + 1)

    if _OBF_HINT.search(text):
        for local, dom in _OBF.findall(text):
            candidates.append(f"{local}@{_DOT_SUB.sub('.', dom)}")

    out = set()
    for c in candidates:
        e = clean_email(c)
        if e:
            out.add(e)
    return out


def email_kind(email: str, site_domains: set[str]) -> str:
    """'domaine_site' (même domaine que le site), 'pro' (domaine d'entreprise) ou 'gratuit' (webmail/FAI)."""
    domain = email.rsplit("@", 1)[1]
    if domain in FREE_EMAIL_DOMAINS:
        return "gratuit"
    if registrable(domain) in site_domains:
        return "domaine_site"
    return "pro"


def is_role(email: str) -> bool:
    local = email.split("@", 1)[0]
    return local.split("+", 1)[0] in ROLE_LOCAL_PARTS


def discover_links(page_html: str, base_url: str, limit: int = 12) -> list[str]:
    """Liens internes vers les pages contact / mentions légales / à propos, triés par pertinence."""
    base_reg = registrable(urlsplit(base_url).hostname or "")
    scores: dict[str, int] = {}
    for n, m in enumerate(_A_OPEN.finditer(page_html[:MAX_SCAN])):
        if n > 3000:
            break
        hm = _HREF.search(m.group(1))
        if not hm:
            continue
        href = html.unescape(next(g for g in hm.groups() if g is not None)).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        tail = page_html[m.end():m.end() + 300]
        close = tail.lower().find("</a")
        anchor = html.unescape(_TAGS.sub(" ", tail[:close] if close >= 0 else tail)).strip()
        try:
            url = urljoin(base_url, href)
            p = urlsplit(url)
        except ValueError:
            continue
        if p.scheme not in ("http", "https") or registrable(p.hostname or "") != base_reg:
            continue
        if _SKIP_EXT.search(p.path):
            continue
        target = f"{unquote(p.path)} {anchor}"
        score = max((s for rx, s in CONTACT_HINTS if rx.search(target)), default=0)
        if score:
            clean = urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, ""))
            scores[clean] = max(scores.get(clean, 0), score)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return [u for u, _ in ranked[:limit]]


def discover_external_sites(page_html: str, base_url: str, limit: int = 30) -> set[str]:
    """Domaines externes d'autres entreprises auto (partenaires, réseau, groupe) cités sur la page."""
    base_reg = registrable(urlsplit(base_url).hostname or "")
    found: set[str] = set()
    for n, m in enumerate(_A_OPEN.finditer(page_html[:MAX_SCAN])):
        if n > 3000 or len(found) >= limit:
            break
        hm = _HREF.search(m.group(1))
        if not hm:
            continue
        href = html.unescape(next(g for g in hm.groups() if g is not None)).strip()
        if not href.startswith(("http://", "https://")):
            continue
        try:
            p = urlsplit(urljoin(base_url, href))
        except ValueError:
            continue
        host = (p.hostname or "").lower()
        reg = registrable(host)
        if not reg or reg == base_reg or domain_in(host, BLOCKED_SITE_DOMAINS) or _SKIP_EXT.search(p.path):
            continue
        tail = page_html[m.end():m.end() + 300]
        close = tail.lower().find("</a")
        anchor = html.unescape(_TAGS.sub(" ", tail[:close] if close >= 0 else tail))
        if _AUTO_LINK.search(f"{anchor} {reg}"):
            found.add(f"{p.scheme}://{p.netloc}/")
    return found
