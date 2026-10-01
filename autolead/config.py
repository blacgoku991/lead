"""Catégories ciblées et listes de référence (secteur automobile, France par défaut)."""
from __future__ import annotations

import re

# Chaque catégorie combine trois façons de trouver des entreprises :
#   naf      : codes APE/NAF interrogés dans la base SIRENE (France)
#   osm      : tags OpenStreetMap (clé, valeur)
#   keywords : requêtes envoyées au moteur de recherche (source "search")
# name_pattern sert à reclasser un "garage" générique d'après son nom.
CATEGORIES: dict[str, dict] = {
    "garage": {
        "label": "Garage, mécanique, entretien",
        "naf": ["45.20A"],
        "osm": [("shop", "car_repair")],
        "keywords": ["garage automobile", "garage mécanique auto", "réparation automobile", "garagiste", "garage agréé", "entretien voiture", "atelier mécanique automobile", "vidange révision voiture", "garage indépendant"],
    },
    "carrosserie": {
        "label": "Carrosserie, peinture, débosselage",
        "naf": [],
        "osm": [],
        "keywords": ["carrosserie automobile", "carrossier peintre automobile", "débosselage sans peinture", "peinture automobile", "réparation carrosserie"],
        "name_pattern": r"carross|d[ée]bossel",
    },
    "vitrage": {
        "label": "Pare-brise, vitrage auto",
        "naf": [],
        "osm": [],
        "keywords": ["remplacement pare-brise", "vitrage automobile", "réparation pare-brise"],
        "name_pattern": r"pare[- ]?brise|vitrage",
    },
    "concession": {
        "label": "Concession, vente VN/VO, mandataire",
        "naf": ["45.11Z"],
        "osm": [("shop", "car")],
        "keywords": ["concessionnaire automobile", "vente voiture occasion", "mandataire automobile", "revendeur automobile", "négociant automobile", "voitures occasion garanties", "dépôt vente voiture", "achat vente automobile", "import voiture", "agent automobile", "vente véhicules neufs"],
    },
    "poids_lourds": {
        "label": "Poids lourds, utilitaires, camions",
        "naf": ["45.19Z", "45.20B"],
        "osm": [("shop", "truck"), ("shop", "truck_repair")],
        "keywords": ["garage poids lourds", "vente camion utilitaire", "utilitaires occasion", "réparation camion"],
        "name_pattern": r"poids[- ]lourds?|\btrucks?\b|camion",
    },
    "pieces": {
        "label": "Pièces détachées, équipements auto",
        "naf": ["45.31Z", "45.32Z"],
        "osm": [("shop", "car_parts")],
        "keywords": ["pièces détachées auto", "équipement automobile magasin", "pièces auto occasion", "accessoires automobile"],
    },
    "pneus": {
        "label": "Pneus, centres auto",
        "naf": ["22.11Z"],
        "osm": [("shop", "tyres")],
        "keywords": ["pneus montage", "centre auto pneus", "pneumatiques", "géométrie parallélisme"],
        "name_pattern": r"\bpneu",
    },
    "moto": {
        "label": "Moto, scooter",
        "naf": ["45.40Z"],
        "osm": [("shop", "motorcycle"), ("shop", "motorcycle_repair")],
        "keywords": ["garage moto", "concessionnaire moto", "moto occasion", "scooter réparation"],
        "name_pattern": r"\bmotos?\b|scooter",
    },
    "location": {
        "label": "Location de véhicules",
        "naf": ["77.11A", "77.11B", "77.12Z"],
        "osm": [("amenity", "car_rental")],
        "keywords": ["location voiture", "location utilitaire", "location véhicule", "location longue durée voiture"],
    },
    "controle_technique": {
        "label": "Contrôle technique",
        "naf": ["71.20A"],
        "osm": [("amenity", "vehicle_inspection")],
        "keywords": ["contrôle technique automobile"],
        "name_pattern": r"contr[ôo]le technique",
    },
    "auto_ecole": {
        "label": "Auto-école",
        "naf": ["85.53Z"],
        "osm": [("amenity", "driving_school")],
        "keywords": ["auto-école"],
    },
    "lavage": {
        "label": "Lavage, detailing",
        "naf": [],
        "osm": [("amenity", "car_wash")],
        "keywords": ["lavage auto", "detailing automobile", "nettoyage voiture", "station de lavage"],
        "name_pattern": r"lavage|\bwash\b|detailing",
    },
    "depannage": {
        "label": "Dépannage, remorquage (NAF 52.21Z : large)",
        "naf": ["52.21Z"],
        "osm": [],
        "keywords": ["dépannage remorquage auto", "dépanneuse", "remorquage véhicule"],
        "name_pattern": r"d[ée]pann|remorqu",
    },
    "casse": {
        "label": "Casse auto, centre VHU",
        "naf": ["38.31Z"],
        "osm": [("industrial", "scrap_yard")],
        "keywords": ["casse automobile", "centre VHU", "épaviste", "rachat voiture épave"],
    },
    "station_service": {
        "label": "Station-service",
        "naf": ["47.30Z"],
        "osm": [("amenity", "fuel")],
        "keywords": ["station service"],
    },
    "camping_car": {
        "label": "Camping-car, caravane",
        "naf": [],
        "osm": [("shop", "caravan")],
        "keywords": ["camping-car vente", "concessionnaire camping-car"],
    },
    "industrie": {
        "label": "Constructeurs, équipementiers",
        "naf": ["29.10Z", "29.20Z", "29.31Z", "29.32Z"],
        "osm": [],
        "keywords": ["équipementier automobile", "préparation automobile", "tuning automobile", "électricité automobile", "climatisation automobile", "reprogrammation moteur"],
    },
}

# Catégories génériques qu'on reclasse d'après le nom ("Carrosserie Dupont" -> carrosserie)
_REFINE_FROM = {"garage", "pieces"}
_NAME_PATTERNS = [
    (cat, re.compile(c["name_pattern"], re.I)) for cat, c in CATEGORIES.items() if c.get("name_pattern")
]


def refine_category(category: str, name: str) -> str:
    if category in _REFINE_FROM and name:
        for cat, rx in _NAME_PATTERNS:
            if rx.search(name):
                return cat
    return category


DEPARTEMENTS = (
    [f"{i:02d}" for i in range(1, 20)]
    + ["2A", "2B"]
    + [f"{i:02d}" for i in range(21, 96)]
    + ["971", "972", "973", "974", "976"]
)

# Villes utilisées par défaut pour la source "search"
DEFAULT_CITIES = [
    "Paris", "Marseille", "Lyon", "Toulouse", "Nice", "Nantes", "Montpellier", "Strasbourg",
    "Bordeaux", "Lille", "Rennes", "Reims", "Toulon", "Saint-Étienne", "Le Havre", "Grenoble",
    "Dijon", "Angers", "Nîmes", "Villeurbanne", "Clermont-Ferrand", "Le Mans", "Aix-en-Provence",
    "Brest", "Tours", "Amiens", "Limoges", "Annecy", "Perpignan", "Boulogne-Billancourt", "Metz",
    "Besançon", "Orléans", "Rouen", "Mulhouse", "Caen", "Nancy", "Argenteuil", "Montreuil",
    "Saint-Denis", "Roubaix", "Tourcoing", "Avignon", "Poitiers", "Pau", "La Rochelle", "Calais",
    "Cannes", "Antibes", "Ajaccio", "Bastia", "Valence", "Troyes", "Chambéry", "Lorient", "Niort",
    "Vannes", "Bayonne", "Colmar", "Quimper",
]

# Préfectures (en plus des grandes villes) : --cities all
PREFECTURES = [
    "Bourg-en-Bresse", "Laon", "Moulins", "Digne-les-Bains", "Gap", "Privas", "Charleville-Mézières",
    "Foix", "Carcassonne", "Rodez", "Aurillac", "Angoulême", "Bourges", "Tulle", "Guéret",
    "Saint-Brieuc", "Périgueux", "Évreux", "Chartres", "Auch", "Lons-le-Saunier", "Mont-de-Marsan",
    "Blois", "Le Puy-en-Velay", "Cahors", "Agen", "Mende", "Saint-Lô", "Châlons-en-Champagne",
    "Chaumont", "Laval", "Bar-le-Duc", "Nevers", "Beauvais", "Alençon", "Arras", "Tarbes",
    "Vesoul", "Mâcon", "Albi", "Montauban", "Évry-Courcouronnes", "Créteil", "Cergy", "Nanterre",
    "Bobigny", "Versailles", "Melun", "Auxerre", "Épinal", "Belfort", "La Roche-sur-Yon", "Draguignan",
    "Basse-Terre", "Fort-de-France", "Cayenne", "Mamoudzou", "Pointe-à-Pitre", "Châteauroux",
    "Saint-Pierre", "Le Tampon", "Dunkerque", "Béziers", "Saint-Nazaire", "Cholet", "Narbonne",
    "Montélimar", "Arles", "Fréjus", "Hyères", "Sète", "Brive-la-Gaillarde", "Saint-Quentin",
    "Bourgoin-Jallieu", "Vienne", "Thionville", "Saint-Malo", "Lens", "Douai", "Valenciennes",
    "Maubeuge", "Compiègne", "Meaux", "Chalon-sur-Saône", "Villefranche-sur-Saône", "Roanne",
    "Annemasse", "Thonon-les-Bains", "Aix-les-Bains", "Salon-de-Provence", "Martigues", "Aubagne",
]

# Webmails / FAI : adresse "non pro"
FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.fr", "yahoo.com", "ymail.com", "hotmail.fr", "hotmail.com",
    "outlook.fr", "outlook.com", "live.fr", "live.com", "msn.com", "orange.fr", "wanadoo.fr", "free.fr",
    "sfr.fr", "neuf.fr", "laposte.net", "bbox.fr", "icloud.com", "me.com", "mac.com", "aol.com",
    "aol.fr", "gmx.fr", "gmx.com", "gmx.de", "protonmail.com", "proton.me", "numericable.fr",
    "club-internet.fr", "aliceadsl.fr", "cegetel.net", "noos.fr", "voila.fr", "mail.com", "yandex.ru",
    "yandex.com", "tiscali.fr", "9online.fr", "nordnet.fr", "dbmail.com", "libertysurf.fr",
    "worldonline.fr", "infonie.fr", "caramail.com", "hotmail.be", "skynet.be", "web.de",
}

# Sites qu'on ne crawle jamais : réseaux sociaux, annuaires, plateformes (CGU anti-scraping
# ou pages qui ne sont pas celles de l'entreprise)
BLOCKED_SITE_DOMAINS = {
    "facebook.com", "fb.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com",
    "tiktok.com", "pinterest.com", "pinterest.fr", "google.com", "google.fr", "goo.gl", "g.page",
    "wa.me", "whatsapp.com", "pagesjaunes.fr", "solocal.com", "societe.com", "pappers.fr",
    "infogreffe.fr", "verif.com", "manageo.fr", "annuaire-entreprises.data.gouv.fr", "data.gouv.fr",
    "yelp.fr", "yelp.com", "tripadvisor.fr", "tripadvisor.com", "mappy.com", "leboncoin.fr",
    "lacentrale.fr", "autoplus.fr", "idgarages.com", "vroomly.com", "wikipedia.org", "118712.fr",
    "kompass.com", "cylex.fr", "hoodspot.fr", "waze.com", "apple.com", "bing.com", "yahoo.com",
    "justacote.com", "starofservice.com", "annuaire.laposte.fr", "118000.fr", "autoscout24.fr",
    "paruvendu.fr", "lefigaro.fr", "ouest-france.fr", "doctolib.fr", "groupon.fr",
}

# Domaines d'e-mails à ignorer : exemples, hébergeurs, prestataires techniques
BAD_EMAIL_DOMAINS = BLOCKED_SITE_DOMAINS | {
    "example.com", "example.fr", "example.org", "exemple.fr", "exemple.com", "sentry.io",
    "wixpress.com", "wix.com", "squarespace.com", "godaddy.com", "ovh.com", "ovh.net",
    "ovhcloud.com", "o2switch.fr", "ionos.fr", "ionos.com", "1and1.fr", "gandi.net",
    "hostinger.com", "jimdo.com", "e-monsite.com", "webself.net", "lws.fr", "amen.fr",
    "online.net", "scaleway.com", "infomaniak.com", "shopify.com", "wordpress.com",
    "automattic.com", "microsoft.com", "cloudflare.com", "cnil.fr", "mailchimp.com",
    "sendinblue.com", "brevo.com", "hubspot.com", "w3.org", "schema.org", "localhost",
}

# Adresses factices type "votre@domaine.fr"
PLACEHOLDER_DOMAIN_RE = re.compile(
    r"^(?:example|exemple|domain|domaine|mondomaine|votredomaine|votre-domaine|monsite|votresite|"
    r"votre-site|mysite|yoursite|yourdomain|email|e-mail|adresse|test|xxx+|site|company|entreprise|"
    r"societe|nomdedomaine|tld)\.[a-z]{2,}$"
)

BAD_EMAIL_TLDS = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico", "avif", "tif", "tiff", "js", "css",
    "json", "xml", "php", "html", "htm", "asp", "aspx", "mp4", "webm", "mp3", "pdf", "zip", "woff",
    "woff2", "ttf", "eot", "map", "min", "local", "lan", "internal", "invalid",
}

BAD_LOCAL_PARTS = {
    "nom", "votre.nom", "votrenom", "votre-nom", "prenom.nom", "nom.prenom", "prenom", "your", "you",
    "yourname", "your.name", "name", "user", "username", "email", "e-mail", "mail", "exemple",
    "example", "test", "xxx", "jean.dupont", "john.doe", "johndoe", "adresse", "votre",
    "votre.email", "votreemail", "votre-email", "monemail",
}
BAD_LOCAL_RE = re.compile(r"^(?:no-?reply|do-?not-?reply|ne-?pas-?repondre|mailer-daemon|postmaster)")

# Adresses génériques (contact@, info@...) : marquées "générique" dans l'export
ROLE_LOCAL_PARTS = {
    "contact", "info", "infos", "information", "accueil", "bonjour", "hello", "commercial", "vente",
    "ventes", "service", "services", "sav", "atelier", "garage", "direction", "compta",
    "comptabilite", "admin", "administration", "secretariat", "rdv", "devis", "pieces", "reception",
    "carrosserie", "magasin", "sales", "support", "office", "location", "concession", "occasion",
    "occasions", "vo", "vn", "apv", "mecanique", "pneus", "boutique", "shop", "rh", "recrutement",
}
