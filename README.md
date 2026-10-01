# AutoLead : e-mails pro du secteur automobile

Bot Python asynchrone qui construit une liste d'e-mails professionnels (domaine pro en priorité) de
**tout le secteur auto** : garages, carrosseries, pare-brise, concessions et mandataires, poids lourds,
pièces détachées, pneus, motos, location, contrôle technique, auto-écoles, lavage, dépannage,
casses/VHU, stations-service, camping-cars, équipementiers.

## Comment ça marche

```
1. collect   Sources des entreprises
             ├─ OpenStreetMap (Overpass) : lieux auto avec site web / e-mail / téléphone
             ├─ SIRENE (API recherche-entreprises.api.gouv.fr) : toutes les entreprises actives par code NAF
             ├─ Brave Search API (optionnel, clé) : "garage automobile Lyon"... -> sites web
             └─ Vos fichiers (.txt / .csv de sites)
2. guess     Pour les fiches SIRENE sans site : garage-dupont.fr, garagedupont.com...
             (seuls les domaines existants sont visités, et retenus si la page contient le CP ou le SIREN)
3. crawl     Visite en parallèle accueil + contact + mentions légales de chaque site
             -> e-mails (mailto, texte, entités HTML, "[at]"/"[dot]", protection Cloudflare)
4. verify    Vérifie que le domaine de l'e-mail a un serveur mail (MX)
5. export    CSV prêt pour Excel, adresses domaine pro en premier
```

Tout est stocké dans une base SQLite (`leads.db`) : **si vous coupez (Ctrl+C), relancez la même
commande et ça reprend où ça s'était arrêté.**

## Installation

Python 3.9+ :

```bash
pip install -r requirements.txt
pip install uvloop   # optionnel, Linux/macOS : un peu plus rapide
```

## Utilisation

```bash
# Tout le secteur auto, France entière (OSM + SIRENE + sites devinés + crawl + MX + export)
python -m autolead run -o leads_auto.csv

# Quelques départements seulement, uniquement les adresses à domaine pro
python -m autolead run --depts 75,92,93,94 --pro-only -o idf.csv

# Seulement garages et carrosseries
python -m autolead run --categories garage,carrosserie,vitrage --depts 69

# Ajouter la recherche web (clé gratuite/payante : https://brave.com/search/api/)
export BRAVE_API_KEY=xxxx
python -m autolead run --cities "Lyon,Villeurbanne,Vénissieux" --search-pages 2

# Vos propres listes de sites (un par ligne, ou CSV avec une colonne site/url/domaine)
python -m autolead run --sources file --file mes_sites.csv

# Étape par étape
python -m autolead collect --depts 13
python -m autolead guess
python -m autolead crawl --concurrency 300
python -m autolead verify
python -m autolead export --pro-only --mx-only --categories garage -o garages.csv
python -m autolead stats
python -m autolead categories     # liste des catégories, codes NAF et tags OSM
```

## Vitesse

- Des centaines de sites visités en parallèle (`--concurrency`, 150 par défaut ; 300-500 sur une
  bonne connexion / un VPS), connexions réutilisées, cache DNS, pages limitées à 1,5 Mo.
- Par site : accueil puis seulement les pages utiles (contact, mentions légales, à propos) ;
  les chemins classiques (`/contact`, `/mentions-legales`...) ne sont tentés que si rien n'a été trouvé.
- Banc d'essai local (2 000 sites, 150 ms de latence simulée) : ~290 sites/s. Sur Internet, comptez
  plutôt 30 à 100 sites/s selon votre débit et la lenteur des sites.
- SIRENE est limitée à 7 requêtes/s par l'État (25 entreprises/requête) : la France entière
  (~250 000 entreprises auto) prend environ 30-40 min. Commencez par quelques départements pour tester.

Réglages utiles : `--max-pages` (6), `--timeout` (15 s/page), `--site-timeout` (60 s/site),
`--retry-failed` (re-tenter les sites injoignables), `--limit N` (test sur N sites).

## Colonnes du CSV

| colonne | contenu |
|---|---|
| `email` | adresse (unique, sauf `--no-dedupe`) |
| `type_email` | `domaine_site` (même domaine que le site, le meilleur), `pro` (autre domaine d'entreprise), `gratuit` (gmail, orange, free...) |
| `generique` | `oui` pour contact@, info@, atelier@... |
| `mx` | `ok` / `invalide` / `non vérifié` |
| `entreprise`, `categorie`, `adresse`, `code_postal`, `ville`, `telephone`, `site_web`, `siren`, `naf`, `source` | fiche de l'entreprise |
| `page_source` | page où l'e-mail a été trouvé |

Séparateur `;` et encodage UTF-8 avec BOM pour une ouverture directe dans Excel (`--sep ,` sinon).

## Ce qui n'est volontairement pas scrapé

Google Maps, PagesJaunes, Facebook, LinkedIn, annuaires (societe.com, pappers...) : leurs conditions
d'utilisation l'interdisent et ils bloquent les robots. L'outil passe par des sources ouvertes (OSM,
SIRENE) et les sites des entreprises eux-mêmes, et respecte `robots.txt`.

## Cadre légal (France / RGPD)

- La prospection B2B par e-mail est autorisée sans consentement préalable **si le message concerne
  l'activité professionnelle du destinataire**. Chaque e-mail doit indiquer l'expéditeur, l'origine
  des données et un **lien de désinscription** fonctionnel ; tenez une liste d'opposition.
- Les adresses nominatives (prenom.nom@) sont des données personnelles : gardez uniquement ce qui sert,
  et supprimez sur demande. La colonne `generique` permet de ne garder que contact@/info@...
- Données OpenStreetMap : licence ODbL (mention « © contributeurs OpenStreetMap » si vous les
  republiez). SIRENE : licence ouverte Etalab.

## Tests

```bash
python -m unittest discover -s tests -t .
```
