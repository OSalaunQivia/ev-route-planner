"""Copilote EV Flotte — proto (France).

Rupture : zéro code de navigation. Le cerveau Python (déjà écrit) calcule l'arrêt
de recharge optimal selon la charge RÉELLE reçue du véhicule (télématique), puis
délègue le turn-by-turn à Google Maps via un deeplink. On scanne le QR → le
téléphone lance la nav vers la borne choisie. Aucune app à installer.

Lancer :  streamlit run copilote.py
(nécessite HERE_API_KEY dans .streamlit/secrets.toml ou .env)
"""
from __future__ import annotations

import os

import requests
import streamlit as st
from dotenv import load_dotenv

from enrichment import enrich_route
from navlink import gmaps_nav_url, qr_url
from pricing import estimate_stop_cost
from providers import TESLA_M3_LR, apply_driving_style, fetch_route_here
from routing import fmt_duration, plan_trip
from stations import apply_filters as filter_stations
from stations import filter_corridor, load_irve

load_dotenv(override=True)

# Véhicule de flotte connecté (valeurs reçues par télématique — ici simulées).
VEHICLE_LABEL = "52 rue de Picpus, 75012 Paris"
VEHICLE_COORDS = (48.846800, 2.394500)

st.set_page_config(page_title="Copilote EV Flotte", page_icon="🔋", layout="centered")


# ---------------- Réutilisation du moteur existant ----------------

def get_secret(name: str) -> str:
    try:
        v = st.secrets.get(name)
        if v:
            return str(v)
    except Exception:
        pass
    return os.getenv(name, "")


@st.cache_data(ttl=86_400, show_spinner="Chargement des bornes IRVE (France)…")
def irve():
    return load_irve()


@st.cache_data(ttl=600, show_spinner=False)
def geocode_fr(q: str):
    """Géocodage France uniquement (Nominatim)."""
    q = q.strip()
    if "," in q:
        try:
            a, b = q.split(",", 1)
            return float(a), float(b)
        except ValueError:
            pass
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": q, "format": "json", "limit": 1, "countrycodes": "fr"},
        headers={"User-Agent": "ev-fleet-copilot/0.1"},
        timeout=15,
    )
    r.raise_for_status()
    res = r.json()
    if not res:
        return None
    return float(res[0]["lat"]), float(res[0]["lon"])


@st.cache_data(ttl=120, show_spinner="Calcul du trajet + arrêt optimal…")
def plan(dest_coords, soc, style):
    """Pipeline complet réutilisé : HERE → enrichissement → corridor IRVE →
    planner d'arrêts. Renvoie (TripPlan, coût €, RouteResult)."""
    model = apply_driving_style(TESLA_M3_LR, style)
    result = fetch_route_here(VEHICLE_COORDS, dest_coords, soc, model, get_secret("HERE_API_KEY"))
    result, _ = enrich_route(result, model, True, True)
    corridor = filter_corridor(irve(), result.points, 5.0)
    df = filter_stations(corridor, categories=["Rapide", "HPC", "Ultra-rapide"])
    tp = plan_trip(result, df, model, initial_soc_pct=soc, mode="fast")
    cost = sum(
        estimate_stop_cost(s.operator, s.power_kw, s.kwh_added)["total_eur"]
        for s in tp.stops
    )
    return tp, cost, result


# ---------------- Interface ----------------

st.title("🔋 Copilote EV Flotte")
st.caption("France · le cerveau choisit la borne, Google Maps conduit.")

st.subheader("📡 Télématique véhicule")
c1, c2 = st.columns(2)
with c1:
    st.metric("Véhicule connecté", "Tesla Model 3 LR")
    st.caption(f"📍 {VEHICLE_LABEL}")
with c2:
    soc = st.slider("Charge réelle reçue (%)", 0, 100, 67)
    style = st.selectbox(
        "Style de conduite détecté", ["Souple", "Normal", "Dynamique"], index=2
    )

dest = st.text_input("🏁 Destination", placeholder="Ex : Place Bellecour, Lyon")
go = st.button("Calculer l'itinéraire", type="primary", use_container_width=True)

if go and dest.strip():
    if not get_secret("HERE_API_KEY"):
        st.error("HERE_API_KEY manquante (.streamlit/secrets.toml ou .env).")
        st.stop()
    coords = geocode_fr(dest)
    if not coords:
        st.error("Adresse introuvable en France.")
        st.stop()
    try:
        tp, cost, result = plan(coords, soc, style)
    except Exception as e:
        st.error(f"Échec du calcul : {e}")
        st.stop()

    st.divider()
    m1, m2, m3 = st.columns(3)
    m1.metric("Distance", f"{result.total_km:.0f} km")
    m2.metric("Batterie arrivée", f"{tp.arrival_soc_pct:.0f}%")
    m3.metric("Temps total", fmt_duration(tp.total_time_s))

    if tp.stops:
        st.success(
            f"⚡ {len(tp.stops)} arrêt(s) recharge · ~{cost:.2f} € · "
            f"{fmt_duration(tp.charge_time_s)} de charge"
        )
        for i, s in enumerate(tp.stops, 1):
            st.markdown(
                f"**{i}. {s.name}** — {s.operator} · {s.city or '—'}  \n"
                f"{s.power_kw:.0f} kW · arrivée {s.soc_arrival_pct:.0f}% → "
                f"repart {s.soc_leave_pct:.0f}% · +{s.kwh_added:.0f} kWh · "
                f"{s.charge_time_min:.0f} min"
            )
    else:
        st.success(f"✅ Aucun arrêt nécessaire — tu arrives à {tp.arrival_soc_pct:.0f}%.")

    if not tp.feasible:
        st.warning(tp.reason)

    nav = gmaps_nav_url(VEHICLE_COORDS, coords, tp.stops)
    st.divider()
    st.link_button(
        "🧭 Naviguer (Google Maps)", nav, type="primary", use_container_width=True
    )
    qcol, tcol = st.columns([1, 2])
    qcol.image(qr_url(nav), caption="Scanne pour naviguer")
    tcol.markdown(
        "**La démo qui tue :** scanne ce QR → Google Maps lance le turn-by-turn "
        "jusqu'à la borne choisie par le cerveau. Zéro app installée, zéro ligne "
        "de code de navigation."
    )
    st.caption(
        "Baisse la *charge réelle* ci-dessus puis recalcule : l'arrêt se déplace "
        "selon la conso. C'est l'anticipation télématique, ton vrai avantage."
    )
