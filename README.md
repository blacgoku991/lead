# AutoLead : contacts du secteur automobile

AutoLead rapproche des établissements, leurs sites et les coordonnées qu'ils publient. Il couvre
les garages, carrosseries, concessions, contrôles techniques et les autres catégories listées par
`python -m autolead categories`.

La base SQLite (`leads.db` par défaut) conserve la provenance des contacts, les associations aux
établissements et les pages restant à examiner. Un contact peut rester **à vérifier** lorsque les
preuves disponibles ne permettent pas de l'attribuer à un établissement précis.

## Installation

Python 3.9 ou ultérieur :

```bash
python -m pip install -r requirements.txt
```

`uvloop` est facultatif sur Linux/macOS (`python -m pip install uvloop`).

## Les étapes

| Commande | Rôle |
|---|---|
| `collect` | Importer les établissements et les URL issus des sources choisies ; enrichir ensuite les SIRET connus avec DINUM si cette source est demandée. |
| `guess` | Proposer des domaines pour les établissements sans site. Leur existence DNS ne suffit pas à confirmer l'identité. |
| `crawl` | Examiner les pages, données structurées et cartes de visite, puis rattacher les contacts lorsque les preuves le permettent. |
| `verify` | Vérifier les enregistrements MX des domaines de messagerie. |
| `export` | Produire un CSV avec les coordonnées, la provenance et l'état du rattachement. |
| `repair-contacts` | Sauvegarder puis réexaminer les anciennes associations et remettre en file les pages concernées. |
| `stats` | Afficher les décomptes de la base, notamment les rattachements, domaines candidats et pages en attente. |

`run` enchaîne collecte, recherche de domaines si applicable, crawl, MX et export. Les options
`--no-guess` et `--no-verify` permettent de désactiver les étapes correspondantes.

## Sources

| Nom dans `--sources` | Données utilisées | Activation |
|---|---|---|
| `osm` | Lieux et coordonnées publiées dans OpenStreetMap, via Overpass. | Dans `auto`. |
| `sirene` | Établissements français trouvés par codes NAF, via l'API Recherche d'entreprises. | Dans `auto` pour la France. |
| `search` | URL trouvées via l'API Brave Search. | Dans `auto` si une clé Brave est configurée. |
| `gmaps` | Fiches via l'API officielle Google Places. | Dans `auto` si une clé Google est configurée. |
| `file` | Liste locale de sites, TXT ou CSV. | Avec `--file`. |
| `dgccrf` | Annuaire officiel des contrôles techniques : identité, adresse, téléphone et URL lorsqu'ils sont renseignés. | Choix explicite dans `--sources`. |
| `dinum` | Jointure SIRET → domaine de messagerie pour les établissements déjà présents. | Choix explicite dans `--sources`. |

Les nouvelles sources `dgccrf` et `dinum` ne sont pas ajoutées à `auto`. Pour utiliser SIRENE puis
ces deux sources dans une même commande, choisir `--sources sirene,dgccrf,dinum`. La jointure
DINUM passe après tous les autres imports de la commande, y compris `--file`.

### DGCCRF : contrôles techniques

Source : [Annuaire des centres de contrôle technique sur data.gouv.fr](https://www.data.gouv.fr/datasets/annuaire-des-centres-de-controle-technique),
diffusé par les ministères économiques et financiers, sous Licence Ouverte 2.0.

L'import conserve les SIRET et les URL complètes des fiches locales. Les centres sont classés dans
`controle_technique`. Les options `--categories` et `--depts` filtrent cet import ; demander uniquement
`--categories garage` ne sélectionne donc aucun contrôle technique. Les champs absents n'effacent
pas les informations déjà conservées. L'annuaire apporte des pistes à explorer, sans fournir de
colonne e-mail.

```bash
python -m autolead collect --sources dgccrf --depts 75,92,93,94
```

### DINUM : domaines candidats

Source : [Domaines email de contact par organisation française](https://www.data.gouv.fr/datasets/domaines-email-de-contact-par-organisation-francaise),
publiée par la Direction interministérielle du numérique sous Licence Ouverte 2.0.

Le fichier expose `SIRET`, `domain_email` et `data_source`. Sa dernière mise à jour affichée au
1er octobre 2026 est le **10 septembre 2024** : ces associations nécessitent une vérification
actuelle. Il contient des domaines de messagerie, pas des adresses e-mail complètes.

La jointure porte sur le SIRET exact des établissements présents dans la base. Elle conserve la
provenance et propose un domaine à vérifier, sans remplacer un site confirmé. Les domaines de
fournisseurs généralistes comme Gmail ou Orange ne sont pas proposés comme sites des garages.
Un domaine de messagerie peut ne pas héberger de site internet.

```bash
python -m autolead collect --sources dinum
```

Pour cette source, `--categories` et `--depts` ne filtrent pas la jointure : elle concerne les SIRET
déjà présents dans la base choisie. Utiliser une base dédiée si un périmètre isolé est nécessaire.

### Instantanés locaux et vérification limitée

Un fichier officiel déjà téléchargé peut être utilisé à la place du téléchargement automatique :

```bash
python -m autolead collect --sources dgccrf,dinum \
  --dgccrf-file /chemin/annuaire-controle-technique.csv \
  --dinum-file /chemin/domaines-organisations.csv
```

DGCCRF accepte les formats CSV et JSON prévus par le collecteur ; DINUM attend un CSV. Les mêmes
contrôles de schéma s'appliquent aux téléchargements et aux fichiers locaux. Conserver ces gros
fichiers de données hors du dépôt Git.

`--source-limit N` plafonne le nombre de **lignes source examinées avant les filtres et la jointure**,
pour chaque source DGCCRF/DINUM. Ce n'est ni un objectif de N contacts ni un échantillon
représentatif. Une petite limite peut produire zéro correspondance DINUM, même sur une base
correcte. Un échec de téléchargement ou de schéma produit une erreur et un code de sortie non nul.
Le téléchargement du fichier reste intégral, même avec cette limite ; le contenu est stocké
temporairement sur disque avant lecture. Pour répéter des essais, utiliser un instantané local.
Une licence ou une date ne sont pas déduites automatiquement du nom d'un fichier local arbitraire.

```bash
python -m autolead collect --db essai.db --sources dgccrf,dinum --source-limit 200
```

## Fiches de réseaux et rattachement

Plusieurs garages peuvent partager le même domaine : chaque fiche conserve sa propre URL et son
identité locale. Le domaine sert à limiter les requêtes, pas à fusionner les établissements.

Le crawler reconnaît les réseaux Top Garage, MOTRIO, AXIAL et Five Star dès la première fiche.
Les autres domaines partagés par au moins deux entreprises bénéficient aussi du mode réseau.
`--deep` active cette exploration pour les autres sites.

Les blocs JSON-LD de type `AutomotiveBusiness`, `AutoRepair` ou `LocalBusiness` et les vCards sont
examinés avec leurs coordonnées. Les contacts d'un garage voisin, du siège du réseau ou d'un
prestataire ne doivent pas être attribués automatiquement au garage principal. Lorsque
l'établissement ne peut pas être identifié, le contact reste à vérifier.

Pour un site deviné, le rapprochement utilise les preuves disponibles : SIRET, nom, adresse,
téléphone et liens avec une fiche connue. Le code postal seul ne confirme pas l'identité. Le SIREN
identifie l'entreprise ; le SIRET identifie un établissement. Leur rôle n'est pas interchangeable.

### Budgets et reprise

`--deep-pages 60` signifie **60 pages au maximum par site et par passage**. Cela ne signifie pas
que tout un réseau est parcouru en un passage. Les URL et sitemaps restant à examiner sont
enregistrés ; un site incomplet porte le statut `partial` et sera repris au prochain `crawl`.
Un site partiel n'est traité qu'une fois par invocation, même lorsque les partenaires sont suivis
sur plusieurs tours. Les sitemaps ont leur propre budget de découverte.

```bash
python -m autolead crawl --concurrency 50 --deep-pages 60
python -m autolead stats
# Relancer pour les pages encore en attente
python -m autolead crawl --concurrency 50 --deep-pages 60
```

Les limites de découverte, les erreurs HTTP et `robots.txt` peuvent empêcher de parcourir certaines
pages. Les compteurs indiquent la progression enregistrée ; ils ne prouvent pas l'exhaustivité d'un
réseau. Un arrêt avec Ctrl+C conserve la progression enregistrée et renvoie le code 130. Relancer
la même commande reprend les travaux encore en attente.

## Mise à jour d'une installation existante sur le VPS

**Arrêter d'abord le processus AutoLead actif** : par exemple `tmux attach -t lead`, puis Ctrl+C.
Vérifier le retour à l'invite avant de mettre à jour le code ou la base. Une seule instance doit
modifier la même base pendant la migration et la réparation.

```bash
cd ~/lead
source .venv/bin/activate
git status --short
```

Si des modifications locales sont affichées, les conserver et les résoudre avant de changer de
branche. Pour récupérer la version corrigée :

```bash
git remote set-branches --add origin codex/establishment-contacts-open-data
git fetch origin
git switch codex/establishment-contacts-open-data
git pull --ff-only
```

L'ouverture d'une ancienne base crée une sauvegarde SQLite avant sa migration. Chaque exécution de
`repair-contacts` crée aussi une sauvegarde avant la réparation ; le chemin est indiqué dans les
journaux. Prévoir l'espace disque pour ces copies et les conserver jusqu'à la validation des
résultats.

Lancer ensuite, depuis Bash et le dossier du projet :

```bash
set -o pipefail
(
  set -e
  python -m autolead repair-contacts --db leads.db
  python -m autolead collect --db leads.db --sources dgccrf,dinum
  python -m autolead crawl --db leads.db --concurrency 150
  python -m autolead verify --db leads.db
  python -m autolead export --db leads.db --attributed-only --no-dedupe -o leads_attribues.csv
  python -m autolead export --db leads.db --no-dedupe -o leads_a_controler.csv
  python -m autolead export --db leads.db --telephones -o telephones.csv
  python -m autolead stats --db leads.db
) 2>&1 | tee -a run.log
```

Le bloc s'arrête si une étape échoue. Dans tmux, Ctrl+B puis D permet de détacher la session.

La réparation conserve les entreprises et les adresses brutes. Elle remet en question les anciennes
validations insuffisantes des sites devinés et planifie le réexamen des fiches concernées. Son bilan
affiche le chemin de sauvegarde, les anciennes validations réinitialisées et les sites/pages remis
en file. Le crawl suivant réalise ce réexamen. Si les statistiques montrent encore des pages en
attente, relancer `crawl`, puis les exports utiles.

`leads_attribues.csv` contient les contacts actuellement rattachés selon les règles du bot.
`leads_a_controler.csv` contient tous les contacts, y compris ceux qui restent incertains : filtrer
sa colonne `etat_rattachement` pour les examiner. Aucun de ces exports ne confirme à lui seul
que chaque boîte e-mail existe encore.

## Autres exemples

```bash
# Collecte automatique : OSM + SIRENE + APIs optionnelles si les clés sont présentes
python -m autolead run --depts 75,92,93,94 -o idf.csv

# Limiter aux garages, carrosseries et vitrages
python -m autolead collect --categories garage,carrosserie,vitrage --depts 69

# Google Places, avec la clé GOOGLE_MAPS_API_KEY déjà configurée
python -m autolead collect --sources gmaps --cities "Lyon,Marseille,Toulouse" --gmaps-pages 3

# Brave Search, avec la clé BRAVE_API_KEY déjà configurée
python -m autolead collect --sources search --cities "Lyon,Villeurbanne,Vénissieux" --search-pages 2

# Liste de sites : un site par ligne, ou un CSV avec une colonne site/url/domaine
python -m autolead run --sources file --file mes_sites.csv

# Traiter les autres établissements sans site, puis leurs candidats
python -m autolead guess --limit 100
python -m autolead crawl --limit 100 --concurrency 20
```

Les clés peuvent aussi être fournies avec `--google-key` ou `--brave-key`. Les quotas et la
facturation dépendent des offres des fournisseurs.

## Réglages et performances

| Option | Valeur par défaut | Effet |
|---|---|---|
| `--concurrency` | 150 | Nombre de sites traités en parallèle. |
| `--max-pages` | 10 | Budget de pages par site et par passage hors mode réseau. |
| `--deep-pages` | 60 | Budget de pages par site et par passage en mode réseau. |
| `--timeout` | 15 s | Délai maximal d'une requête de page. |
| `--site-timeout` | 60 s | Délai maximal d'un passage sur un site. |
| `--dns-concurrency` | 300 | Nombre de requêtes DNS simultanées pour les commandes concernées. |
| `--limit N` | Sans limite | Nombre maximal de sites ou d'établissements à traiter selon la commande. |
| `--retry-failed` | Désactivé | Remettre en file les sites auparavant injoignables. |
| `--recrawl-no-email` | Désactivé | Remettre en file les sites déjà visités où aucun e-mail n'a été trouvé. |
| `--no-partners` | Désactivé | Désactiver l'exploration des domaines partenaires découverts. |

Les nombres de pages, concurrences et limites doivent être des entiers strictement positifs. Les
délais et débits doivent être finis et strictement positifs. Le débit réel dépend des sites, des
quotas, de `robots.txt`, des délais et du VPS. Les mesures sur des serveurs de test locaux ne
permettent pas d'annoncer une durée ou un débit de collecte sur Internet. Commencer par un
échantillon et consulter les erreurs, la mémoire et la charge avant d'augmenter la concurrence.

## Exports et lecture des résultats

Le CSV utilise `;` et UTF-8 avec BOM pour Excel (`--sep ,` pour changer le séparateur).

| Colonne | Contenu |
|---|---|
| `email` | Adresse trouvée. |
| `type_email` | `domaine_site`, `pro` ou `gratuit` : classification du domaine de messagerie. |
| `generique` | `oui` pour des formes comme contact@ ou atelier@ ; sinon `non`. |
| `mx` | `ok`, `invalide` ou `non vérifié` au niveau du domaine. |
| `entreprise`, `categorie`, `adresse`, `code_postal`, `ville` | Identité de l'établissement lorsqu'un rattachement est disponible. |
| `telephone`, `telephone_site`, `site_web`, `siren`, `siret`, `naf`, `source` | Coordonnées et identifiants rattachés à cette fiche. |
| `page_source` | URL où le contact a été trouvé. |
| `etat_rattachement` | Nature ou absence du rattachement, détaillée ci-dessous. |
| `preuve_rattachement` | Éléments enregistrés pour justifier l'association. |
| `type_source_contact` | Type de source dont provient le contact. |
| `date_collecte` | Date de collecte du contact conservée dans la base. |
| `derniere_verification` | Date de la dernière vérification MX du domaine, lorsqu'elle est disponible. |
| `nombre_etablissements` | Nombre d'établissements distincts actuellement associés à cette adresse dans la base. |

| `etat_rattachement` | Interprétation |
|---|---|
| `source` | Contact fourni explicitement avec une fiche d'établissement dans la source. |
| `structured` | Contact associé à une fiche identifiée par ses données structurées. |
| `site_unique` | Contact rattaché par un site indépendant lié à un établissement unique selon les règles du bot. |
| `unverified` | Rattachement insuffisant ou ambigu : à vérifier. |

`--attributed-only` filtre les contacts dont le rattachement satisfait les règles actuelles. Une
source peut néanmoins être ancienne ou erronée : conserver les preuves facilite la vérification.

Par défaut, l'export déduplique les adresses e-mail. `--no-dedupe` conserve leurs associations
distinctes avec les établissements ; une adresse centrale partagée n'est ainsi pas présentée
comme plusieurs nouvelles adresses uniques.

Les adresses Gmail, Hotmail, Orange ou Wanadoo peuvent être publiées comme contacts professionnels.
Elles sont conservées par défaut. `--pro-only` les exclut selon leur domaine ; ce filtre ne prouve
pas le caractère professionnel des autres adresses. Utiliser `--attributed-only` pour filtrer le
rattachement plutôt que le fournisseur de messagerie.

`verify` et `--mx-only` concernent les serveurs mail du **domaine**. Un MX valide ne vérifie ni
l'existence d'une boîte précise, ni la réception d'un message, ni son classement en boîte de
réception. `--telephones` produit un export séparé des entreprises ayant un téléphone ;
`--attributed-only` s'applique uniquement à l'export e-mails.

## Provenance des sources

Les licences et conditions propres aux sources restent applicables à leur réutilisation. Conserver
les URL sources et les dates facilite leur attribution et les corrections. Références :

- [Licence et attribution OpenStreetMap](https://www.openstreetmap.org/copyright).
- [API Recherche d'entreprises](https://recherche-entreprises.api.gouv.fr/docs/).
- [DGCCRF : annuaire officiel et licence](https://www.data.gouv.fr/datasets/annuaire-des-centres-de-controle-technique).
- [DINUM : domaines, date de mise à jour et licence](https://www.data.gouv.fr/datasets/domaines-email-de-contact-par-organisation-francaise).

## Tests

```bash
python -m unittest discover -s tests -t .
```

Les tests utilisent des fixtures, des fichiers temporaires et des serveurs HTTP locaux. Ils
vérifient le comportement du code, sans établir la disponibilité actuelle de toutes les sources
publiques. Pour évaluer le gain réel, comparer sur une même base les nouveaux établissements avec
contact attribué, les adresses uniques, les associations corrigées et les cas restant à vérifier.
