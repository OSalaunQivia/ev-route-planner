# Qivia EV Route Planner

Planificateur d'itinéraire pour véhicule électrique (France) : trajet via **HERE
Routing** avec courbe de consommation EV, correction **météo + relief**,
sélection des **bornes IRVE** le long du corridor, et planification des **arrêts
de recharge optimaux** pour arriver au niveau de batterie visé. UI **Streamlit**
mobile-first ; cœur métier en modules Python réutilisables.

## Démarrage rapide

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # puis renseigner HERE_API_KEY
streamlit run app.py
```

`HERE_API_KEY` est obligatoire. `TOMTOM_API_KEY` et `ACCESS_PASSWORD_HASH` sont
optionnels (sans hash, l'app n'est pas protégée par mot de passe). Le premier
calcul télécharge les bornes IRVE (~30 Mo, cache 24 h dans `data/`).

## Documentation

| Document | Pour qui / quoi |
|---|---|
| **[`HANDOFF.md`](HANDOFF.md)** | **Passation développeur** : install, déploiement, et intégration du moteur dans une app plus large. **Commencer ici.** |
| [`Qivia_EV_Documentation.md`](Qivia_EV_Documentation.md) | Doc technique de fond : algorithmes, modèle de données, sources/coûts API, conformité RGPD, roadmap. |
| [`CONTEXT.md`](CONTEXT.md) | Note de reprise interne (historique du projet). |

## Structure

- **UI** : `app.py` (app complète), `copilote.py` (démo flotte + template d'appel du moteur).
- **Moteur** (Python pur, réutilisable hors Streamlit) : `providers.py`, `routing.py`,
  `enrichment.py`, `stations.py`, `pricing.py`, `availability.py`, `navlink.py`.

Détails et schéma d'architecture dans [`HANDOFF.md`](HANDOFF.md).

## Déploiement

Cible actuelle : **Streamlit Community Cloud** (déploiement auto sur `git push`,
secrets dans le panneau Settings). Auto-hébergement et wrapper PWA décrits dans
[`HANDOFF.md`](HANDOFF.md) §5.
