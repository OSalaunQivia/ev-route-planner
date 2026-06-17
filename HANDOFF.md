# Qivia EV Route Planner — Guide de passation développeur

> Document destiné au développeur qui reprend le code pour l'intégrer dans une
> application plus large. Il couvre l'installation, le déploiement, et surtout
> **comment réutiliser le moteur de calcul sans la couche Streamlit**.
>
> Pour le détail des **algorithmes, sources de données, coûts API, conformité
> RGPD et roadmap**, voir [`Qivia_EV_Documentation.md`](Qivia_EV_Documentation.md)
> (documentation technique de fond — non dupliquée ici).
> `CONTEXT.md` est une note de reprise interne, pas une doc d'intégration.

---

## 1. En une phrase

Planificateur d'itinéraire pour véhicule électrique (France) : il calcule un
trajet via **HERE Routing** (avec courbe de consommation EV), enrichit la
consommation par la **météo et le relief**, sélectionne les **bornes de
recharge IRVE** le long du corridor, et planifie les **arrêts de recharge
optimaux** pour arriver à destination à un niveau de batterie cible.

L'app livrée est une UI **Streamlit** mobile-first, mais le cœur métier (« le
cerveau ») est un ensemble de modules Python purs, réutilisables tels quels.

---

## 2. Architecture : deux couches nettement séparées

```
┌─────────────────────────────────────────────────────────────┐
│  COUCHE UI (Streamlit) — remplaçable                         │
│    app.py        wizard input→loading→result, carte Folium,  │
│                  thème Qivia, gate password, géoloc          │
│    copilote.py   2e UI minimale (démo flotte + QR nav)       │
├─────────────────────────────────────────────────────────────┤
│  COUCHE MOTEUR (« le cerveau ») — Python pur, réutilisable   │
│    providers.py   modèle EV + appels HERE/TomTom → RouteResult│
│    enrichment.py  météo (Open-Meteo) + relief (OpenTopoData) │
│    stations.py    bornes IRVE (data.gouv) + corridor numpy   │
│    routing.py     plan_trip() → arrêts de recharge (TripPlan)│
│    pricing.py     estimation €/kWh par opérateur             │
│    availability.py disponibilité temps réel (TomTom)         │
│    navlink.py     deeplinks Google Maps / Waze + QR          │
└─────────────────────────────────────────────────────────────┘
```

**Point clé pour l'intégration :** les modules de la couche moteur ne dépendent
PAS de Streamlit (sauf `availability.py` et la mise en cache, voir §7). Vous
pouvez les appeler depuis n'importe quel backend (FastAPI, Flask, job batch,
etc.). `copilote.py` est l'exemple canonique de cet usage headless (§6).

---

## 3. Inventaire des fichiers

| Fichier | Rôle |
|---|---|
| `app.py` | UI Streamlit principale (~1900 l.). Wizard, carte, thème, auth, géoloc, saisie d'adresse. |
| `copilote.py` | UI Streamlit secondaire = **template d'appel du moteur** (démo flotte). |
| `providers.py` | Modèle `TESLA_M3_LR`, `DRIVING_STYLES`, `apply_driving_style`, `fetch_route_here`, `fetch_route_tomtom`, `geocode`. Dataclasses `RoutePoint`, `RouteResult`. |
| `routing.py` | `plan_trip()` (planner greedy lookback), `fmt_duration`. Dataclasses `ChargingStop`, `TripPlan`. |
| `enrichment.py` | `enrich_route()`, `fetch_weather`, `fetch_elevations`. |
| `stations.py` | `load_irve`, `filter_corridor`, `apply_filters`, `categorize_power`, `top_operators`. |
| `pricing.py` | `estimate_price_per_kwh`, `estimate_stop_cost`, `parse_irve_tarification`. |
| `availability.py` | `fetch_availability` (TomTom EV, cache 2 min). |
| `navlink.py` | `gmaps_nav_url`, `waze_nav_url`, `find_place_id`, `qr_url`. |
| `requirements.txt` | Dépendances Python. |
| `.streamlit/config.toml` | Thème dark Qivia. |
| `.streamlit/secrets.toml.example` | Modèle de secrets (clés API + hash mot de passe). |
| `.env.example` | Modèle d'env local (clés API). |
| `assets/` | Logos Qivia, icônes Google Maps / Waze / PWA. |
| `data/irve.csv` | Cache local des bornes IRVE (re-téléchargé si absent ; gitignoré). |
| `docs/` | Wrapper **PWA** (GitHub Pages) : `index.html`, `manifest.json`, icône. |
| `Qivia_EV_Documentation.md/.pdf/.html/.docx` | Doc technique de fond + exports. |
| `build_doc.py` | Génère l'export `.html` (prêt PDF/Word) depuis le `.md`. Outil dev : `pip install markdown`. |
| `CONTEXT.md` | Note de reprise interne (historique). |

---

## 4. Installation (développement local)

**Prérequis :** Python **3.9+** (testé sur 3.9.6), `git`.

```bash
git clone <repo-url> ev-route-planner
cd ev-route-planner

python -m venv .venv
source .venv/bin/activate            # Windows : .venv\Scripts\activate
pip install -r requirements.txt
```

Dépendances (`requirements.txt`) :
`streamlit`, `folium`, `streamlit-folium`, `streamlit-js-eval`, `requests`,
`python-dotenv`, `flexpolyline`, `pandas`, `numpy`.
*(Plus de `streamlit-searchbox` — retiré, voir §9.)*

### Secrets / variables d'environnement

| Variable | Obligatoire | Usage |
|---|---|---|
| `HERE_API_KEY` | **Oui** | Routing EV (cœur du calcul). |
| `TOMTOM_API_KEY` | Optionnel | Variante de routing + disponibilité bornes. |
| `ACCESS_PASSWORD_HASH` | Optionnel | SHA-256 du mot de passe d'accès. **Si vide/absent, l'app n'est pas protégée.** |

En local, créez `.env` (cf. `.env.example`) **ou** `.streamlit/secrets.toml`
(cf. `.streamlit/secrets.toml.example`). Les deux sont gitignorés.

Générer le hash du mot de passe :
```bash
python3 -c "import hashlib,getpass; print(hashlib.sha256(getpass.getpass('Mot de passe : ').encode()).hexdigest())"
```

### Lancer

```bash
streamlit run app.py          # app complète (port 8501 par défaut)
streamlit run copilote.py     # démo flotte / template moteur
```

> Sans `ACCESS_PASSWORD_HASH`, le gate est désactivé et vous arrivez direct sur
> le formulaire — pratique en dev. La saisie d'adresse utilise **Photon**
> (gratuit, sans clé). Le premier calcul télécharge les bornes IRVE (~30 Mo,
> mis en cache 24 h dans `data/`).

---

## 5. Déploiement

### 5.1 Streamlit Community Cloud (cible actuelle / prod)

1. Pousser le repo sur GitHub.
2. Sur [share.streamlit.io](https://share.streamlit.io) → **New app** → pointer
   sur le repo, branche `main`, fichier `app.py`.
3. **Settings → Secrets** : coller le contenu de `secrets.toml.example` avec les
   vraies valeurs (clés API + hash). Ne **jamais** committer le fichier réel.
4. Déploiement auto à chaque `git push` sur `main` (~30–60 s).

Contraintes du tier gratuit (assumées) : footer « Built with Streamlit » non
supprimable ; pipeline ~13 s typique (dominé par la latence HERE).

### 5.2 Wrapper PWA (GitHub Pages) — optionnel

`docs/` contient un wrapper installable (splash + redirect vers l'URL
Streamlit). Activer **GitHub Pages** sur le dossier `docs/` de la branche
`main`. ⚠️ Limite connue : le redirect cross-origin `github.io → streamlit.app`
casse le mode standalone PWA sur iOS (le chrome de Safari réapparaît). Documenté,
pas de fix gratuit — à remplacer par un hébergement same-origin si besoin.

### 5.3 Auto-hébergement (recommandé pour intégration produit)

L'app est un process Streamlit standard, hébergeable partout (Docker, VM, PaaS) :

```bash
streamlit run app.py --server.port 8080 --server.headless true \
  --browser.gatherUsageStats false
```

Passer les secrets par variables d'environnement (l'app lit `os.getenv` en
fallback de `st.secrets`). Mettre l'app derrière un reverse proxy (TLS, auth).
Le téléchargement IRVE nécessite un accès sortant vers `data.gouv.fr`.

Exemple de `Dockerfile` minimal à ajouter si besoin :
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8080
CMD ["streamlit","run","app.py","--server.port=8080","--server.headless=true","--browser.gatherUsageStats=false"]
```

---

## 6. Intégrer le moteur dans une app plus large (sans UI)

Le pipeline complet, extrait de `copilote.py` (template de référence) :

```python
from providers import TESLA_M3_LR, apply_driving_style, fetch_route_here
from enrichment import enrich_route
from stations import load_irve, filter_corridor, apply_filters as filter_stations
from routing import plan_trip, fmt_duration
from pricing import estimate_stop_cost
from navlink import gmaps_nav_url

origin = (48.846800, 2.394500)      # (lat, lng)
dest   = (45.7640, 4.8357)          # Lyon
soc    = 67                          # % batterie au départ
style  = "Dynamique"                 # "Souple" | "Normal" | "Dynamique"

model  = apply_driving_style(TESLA_M3_LR, style)

# 1) Itinéraire + profil de consommation (HERE)
result = fetch_route_here(origin, dest, soc, model, HERE_API_KEY)

# 2) Enrichissement météo + relief  (signature: (result, model, weather, elevation))
result, _ = enrich_route(result, model, True, True)

# 3) Bornes IRVE dans un corridor de 5 km autour du trajet
corridor = filter_corridor(load_irve(), result.points, 5.0)
df = filter_stations(corridor, categories=["Rapide", "HPC", "Ultra-rapide"])

# 4) Planner d'arrêts de recharge
plan = plan_trip(result, df, model, initial_soc_pct=soc, mode="fast")

# 5) Coût + deeplink navigation
cost = sum(estimate_stop_cost(s.operator, s.power_kw, s.kwh_added)["total_eur"]
           for s in plan.stops)
nav_url = gmaps_nav_url(origin, dest, plan.stops)
```

### Types de retour (à sérialiser pour une API JSON)

`RouteResult` (providers.py) :
`provider, points[RoutePoint], total_km, total_consumption_kwh,
soc_at_arrival_pct, first_below_10pct, total_duration_s, total_toll_eur,
avoids_tolls`.
`RoutePoint` : `lat, lng, km, soc_pct, speed_kmh, kwh_consumed_from_start`.

`TripPlan` (routing.py) :
`stops[ChargingStop], feasible, reason, arrival_soc_pct, drive_time_s,
charge_time_s, total_time_s, updated_points, mode`.
`ChargingStop` : `km, lat, lng, name, operator, city, power_kw,
soc_arrival_pct, soc_leave_pct, kwh_added, charge_time_min`.

Ce sont des `@dataclass` → `dataclasses.asdict(obj)` les rend directement
JSON-sérialisables.

### Paramètres utiles

- `plan_trip(..., mode="fast"|"eco", min_soc_pct=10.0, max_stops=12, **overrides)`.
- `fetch_route_here(..., avoid_tolls=False)` → calculer avec/sans péage.
- Géocodage : `providers.geocode()` (générique) ou, côté UI, Photon/Nominatim.

### Découpler de Streamlit

Les modules moteur sont du Python pur **sauf** :
- La **mise en cache** : dans l'app c'est `@st.cache_data` (IRVE 24 h, météo, dispo
  2 min). Hors Streamlit, remplacez par votre propre cache (`functools.lru_cache`,
  Redis, fichier). `load_irve()` lui-même n'est pas décoré — c'est l'appelant qui
  cache.
- `availability.fetch_availability` lit la clé via Streamlit/env ; injectez la
  clé explicitement si vous le sortez du contexte Streamlit.

---

## 7. Données externes, clés, licences (résumé)

| Source | Usage | Clé | Notes |
|---|---|---|---|
| HERE Routing | itinéraire + conso EV | **Oui** | Goulot perf (~5–8 s/req). |
| TomTom | routing alt + dispo bornes | Optionnel | Dispo cachée 2 min. |
| Photon (komoot) | autocomplétion adresse (UI) | Non | Sans clé. |
| Nominatim (OSM) | géocodage FR (copilote) | Non | Respecter le User-Agent + quotas. |
| Open-Meteo | météo | Non | Gratuit. |
| OpenTopoData | élévation | Non | Batch 60 pts max. |
| data.gouv IRVE | bornes (~140k) | Non | Filtrer `etalab/schema-irve-statique`. |

Détail des coûts, quotas et obligations légales (RGPD géoloc, zones de danger,
licences ODbL) : **§4, §5, §8 de `Qivia_EV_Documentation.md`**.

---

## 8. Sécurité

- Secrets **jamais** committés (`.env`, `.streamlit/secrets.toml` gitignorés).
- Gate d'accès = SHA-256 d'un mot de passe partagé ; auth persistée via query
  param `?auth=ok` (contourne les resets de session iOS). **Ce n'est pas une
  vraie authentification multi-utilisateur** — à remplacer par un vrai SSO/IdP
  pour une intégration produit.
- Pas de PII stockée ; la géoloc est lue côté navigateur à la demande.

---

## 9. Décisions & pièges connus (à ne pas refaire)

- **Saisie d'adresse = champ Streamlit natif** (`native_address_field` dans
  `app.py`), PAS le composant `streamlit-searchbox`. Ce composant (react-select
  en iframe) était instable sur mobile : un rerun externe (ex. géoloc qui se
  résout) effaçait le texte saisi et cassait la sélection. Le champ natif
  conserve sa valeur dans `session_state` à travers les reruns → immunisé.
  **Ne pas réintroduire le composant ni patcher du JS minifié.**
- Conséquence UX : les suggestions s'affichent **après validation** du champ
  (Entrée / blur), pas à chaque frappe. C'est le compromis pour la fiabilité.
- `RoutePoint.kwh_consumed_from_start` est **non clampé** (peut être « négatif »
  en SoC virtuel) — nécessaire au planner ; ne pas le clamper.
- IRVE CSV : filtrer sur `etalab/schema-irve-statique` (sinon on récupère des
  rapports de validation).
- `pricing` : `isinstance(x, str)` obligatoire avant parsing (pandas met des
  `NaN` flottants partout).
- TomTom : champ conso = `batteryConsumptionInkWh` (pas `consumptionInkWh`).
- `st.popover` ne se ferme pas sur clic interne → utiliser `st.dialog` + `st.rerun()`.
- iOS : `session_state` peut se vider (sleep d'onglet) → d'où le `?auth=ok`.

---

## 10. Pour aller plus loin

- **Doc technique de fond** (algorithmes, modèle de données champ par champ,
  coûts, conformité, roadmap en jalons) : [`Qivia_EV_Documentation.md`](Qivia_EV_Documentation.md).
- **Première étape d'intégration recommandée** (cf. « Jalon 0 » de la doc) :
  exposer le pipeline du §6 derrière une API (FastAPI), en remplaçant les
  caches Streamlit par un cache serveur, puis brancher la couche UE existante
  dessus. `copilote.py` montre déjà l'appel bout-en-bout.
```
