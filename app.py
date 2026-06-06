"""Streamlit app: HERE EV routing + charging stops planner.

Mobile-first layout: single centered column, expanders for inputs, no sidebar.
"""
from __future__ import annotations

import hashlib
import os

import folium
import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from streamlit_folium import st_folium
from streamlit_searchbox import st_searchbox

from availability import fetch_availability
from enrichment import enrich_route
from pricing import estimate_price_per_kwh, estimate_stop_cost
from providers import (
    DRIVING_STYLES,
    TESLA_M3_LR,
    RouteResult,
    apply_driving_style,
    fetch_route_here,
)
from routing import TripPlan, fmt_duration, plan_trip
from stations import (
    apply_filters as filter_stations,
    filter_corridor,
    load_irve,
)


# ============================================================================
# Caches and helpers
# ============================================================================

load_dotenv(override=True)


def get_secret(name: str, default: str = "") -> str:
    """Streamlit Cloud secrets first, then local env."""
    try:
        val = st.secrets.get(name)
        if val:
            return str(val)
    except Exception:
        pass
    return os.getenv(name, default)


@st.cache_data(ttl=86_400, show_spinner="Téléchargement IRVE (~30 Mo, une seule fois)…")
def get_irve_cached():
    return load_irve()


@st.cache_data(ttl=300, show_spinner=False)
def photon_search(query: str) -> list[tuple[str, str]]:
    """Photon (OSM) autocomplete. Returns (label, 'lat,lng') tuples."""
    if not query or len(query.strip()) < 2:
        return []
    try:
        r = requests.get(
            "https://photon.komoot.io/api",
            params={"q": query, "lang": "fr", "limit": 6},
            timeout=5,
            headers={"User-Agent": "ev-route-planner/0.1"},
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[photon] {e}")
        return []
    # OSM categories that already represent a whole locality — no need to add
    # a postcode (Photon returns a semicolon-joined list of all postcodes anyway).
    LOCALITY_TYPES = {
        "city", "town", "village", "hamlet", "municipality",
        "suburb", "neighbourhood", "state", "region", "country",
    }
    results = []
    for f in data.get("features", []):
        props = f.get("properties", {})
        lng, lat = f["geometry"]["coordinates"]
        name = (props.get("name") or "").strip()
        street = (props.get("street") or "").strip()
        housenumber = (props.get("housenumber") or "").strip()
        city = (props.get("city") or "").strip()
        postcode = (props.get("postcode") or "").strip()
        country = (props.get("country") or "").strip()
        osm_value = (props.get("osm_value") or "").lower()

        # Drop messy multi-postcode values like "06000;06100;06200;06300".
        if ";" in postcode:
            postcode = ""

        # Build the primary label part. For houses, prefer "52 Rue Laugier"
        # over the OSM `name` field (which may be empty or generic).
        if housenumber and street:
            primary = f"{housenumber} {street}"
        elif street and not name:
            primary = street
        else:
            primary = name or street

        parts: list[str] = []
        if primary:
            parts.append(primary)
        # Only add postcode/city detail for street-level or specific places,
        # not for whole cities/regions.
        if osm_value not in LOCALITY_TYPES:
            loc_bits = []
            if postcode:
                loc_bits.append(postcode)
            if city and city != primary:
                loc_bits.append(city)
            if loc_bits:
                parts.append(" ".join(loc_bits))
        if country:
            parts.append(country)
        label = ", ".join(parts) if parts else f"{lat:.3f}, {lng:.3f}"
        results.append((label, f"{lat:.6f},{lng:.6f}"))
    return results


def _parse_coords(value: str | None) -> tuple[float, float] | None:
    if not value or "," not in value:
        return None
    try:
        a, b = value.split(",", 1)
        return float(a), float(b)
    except ValueError:
        return None


def soc_color(soc: float) -> str:
    if soc > 50:
        return "#22c55e"
    if soc > 25:
        return "#eab308"
    if soc > 10:
        return "#f97316"
    return "#dc2626"


# ============================================================================
# Page config + CSS — mobile-first, single centered column, no sidebar
# ============================================================================

st.set_page_config(
    page_title="EV Route Planner",
    page_icon="assets/apple-touch-icon.png",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# PWA / iOS home-screen hints — apple-touch-icon, dark status bar, short title.
st.markdown(
    """
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="Qivia EV">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="theme-color" content="#03060D">
    <link rel="apple-touch-icon" href="assets/apple-touch-icon.png">
    <link rel="apple-touch-icon" sizes="180x180" href="assets/apple-touch-icon.png">
    <link rel="icon" type="image/png" href="assets/apple-touch-icon.png">
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap');

    /* Base */
    .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
        background-color: #03060D !important;
        color: #FFFFFF;
    }
    /* Plus Jakarta Sans only on textual elements — NOT on span/div which
       Streamlit also uses for icon containers. */
    html, body, .stApp, .stMarkdown, .stCaption,
    p, label, button, input, textarea, select,
    h1, h2, h3, h4, h5, h6 {
        font-family: 'Plus Jakarta Sans', -apple-system, sans-serif !important;
    }
    h1, h2, h3, h4 {
        font-weight: 700 !important;
        letter-spacing: -0.02em;
        line-height: 1.15;
        color: #FFFFFF !important;
    }
    h1 { font-size: 2.2rem !important; }

    /* Qivia mint accents */
    .qivia-highlight {
        color: #00E676;
        text-shadow: 0 0 12px rgba(0, 230, 118, 0.6), 0 0 28px rgba(0, 230, 118, 0.27);
    }
    .qivia-accent {
        height: 4px;
        width: 64px;
        background: #5FFFA7;
        border-radius: 4px;
        margin: 0.4rem 0 1rem 0;
    }

    /* Hide Streamlit chrome — no sidebar to toggle, so we don't need the header */
    header[data-testid="stHeader"] { display: none !important; }
    div[data-testid="stToolbar"] { display: none !important; }

    /* Tighten the top padding so content starts near the top */
    .main .block-container,
    section[data-testid="stMain"] > div:first-child,
    [data-testid="stAppViewContainer"] .block-container {
        padding-top: 1.2rem !important;
        padding-bottom: 2rem !important;
    }

    /* Inputs (text, password, etc.) */
    input, textarea {
        background-color: #0B111C !important;
        color: #FFFFFF !important;
        border-color: #1A2030 !important;
    }
    [data-testid="stWidgetLabel"], label {
        color: #9AA3B2 !important;
        font-size: 0.85rem;
        font-weight: 500;
    }

    /* Expanders */
    [data-testid="stExpander"] {
        background-color: #0B111C;
        border: 1px solid #1A2030;
        border-radius: 10px;
        margin-bottom: 0.6rem;
    }
    [data-testid="stExpander"] summary {
        color: #FFFFFF !important;
        font-weight: 600;
    }
    [data-testid="stExpander"] summary p {
        color: #FFFFFF !important;
        font-weight: 600;
    }

    /* Primary button: mint, full width */
    .stButton > button[kind="primary"] {
        background-color: #5FFFA7 !important;
        color: #03060D !important;
        border: none !important;
        font-weight: 700 !important;
        border-radius: 10px !important;
        padding: 0.85rem 1.2rem !important;
        font-size: 1.05rem !important;
        width: 100%;
    }
    .stButton > button[kind="primary"]:hover {
        background-color: #00E676 !important;
    }
    .stButton > button { border-radius: 8px !important; }

    /* Metric cards */
    div[data-testid="stMetric"] {
        background: #0B111C;
        border: 1px solid #1A2030;
        border-radius: 10px;
        padding: 0.7rem 0.9rem;
    }
    div[data-testid="stMetricLabel"] { color: #9AA3B2 !important; }
    div[data-testid="stMetricValue"] {
        color: #5FFFA7 !important;
        font-weight: 700 !important;
    }

    /* Folium iframe blends into the dark page */
    iframe { background-color: #03060D !important; }

    /* streamlit-searchbox dropdown styling — proper container + spacing for options. */
    [class*="-menu"] {
        background-color: #0B111C !important;
        border: 1px solid #2A3344 !important;
        border-radius: 8px !important;
        margin-top: 4px !important;
        overflow: hidden !important;
        box-shadow: 0 4px 12px rgba(0,0,0,0.4) !important;
    }
    [class*="-menuList"] {
        background-color: #0B111C !important;
        max-height: 280px !important;
        padding: 4px 0 !important;
    }
    [class*="-option"] {
        background-color: #0B111C !important;
        color: #FFFFFF !important;
        padding: 10px 14px !important;
        cursor: pointer !important;
        border-bottom: 1px solid #1A2030 !important;
        font-size: 0.92rem !important;
        line-height: 1.3 !important;
        white-space: nowrap !important;
        overflow: hidden !important;
        text-overflow: ellipsis !important;
    }
    [class*="-option"]:last-child { border-bottom: none !important; }
    [class*="-option"]:hover,
    [class*="-option--is-focused"] {
        background-color: #1A2030 !important;
    }
    [class*="-control"] {
        background-color: #0B111C !important;
        color: #FFFFFF !important;
        border-color: #2A3344 !important;
    }

    /* Tabs styling */
    button[role="tab"] {
        color: #9AA3B2 !important;
        font-weight: 600 !important;
    }
    button[role="tab"][aria-selected="true"] {
        color: #5FFFA7 !important;
        border-bottom-color: #5FFFA7 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================================
# Password gate + header
# ============================================================================

def _render_header() -> None:
    col_logo, col_title = st.columns([1, 5])
    with col_logo:
        st.image("assets/logo-qivia.png", width=70)
    with col_title:
        st.markdown(
            '<h1 style="margin-top:0.4rem;font-size:1.6rem;">'
            'Bonjour <span class="qivia-highlight">Arthur</span>,<br>'
            'où souhaitez-vous aller aujourd\'hui ?'
            '</h1>',
            unsafe_allow_html=True,
        )
    st.markdown('<div class="qivia-accent"></div>', unsafe_allow_html=True)


def _check_password() -> None:
    if st.session_state.get("auth_ok"):
        return
    expected = get_secret("ACCESS_PASSWORD_HASH")
    if not expected:
        return
    _render_header()
    st.markdown(
        """
        <style>
        div[data-testid="stTextInput"] input {
            border: 2px solid #5FFFA7 !important;
            padding: 0.75rem 1rem !important;
            font-size: 1.05rem !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    # Hide the "Press Enter to apply" hint Streamlit shows on text inputs.
    st.markdown(
        """
        <style>
        [data-testid="InputInstructions"],
        [data-testid="stTextInput"] [class*="InputInstructions"],
        div[data-testid="stTextInput"] div[class*="instructions"] {
            display: none !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    pwd = st.text_input(
        "🔒 Code d'accès",
        type="password",
        key="access_pwd",
    )
    if pwd:
        if hashlib.sha256(pwd.encode()).hexdigest() == expected:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Code invalide")
    st.stop()


_check_password()


# ============================================================================
# Wizard state — 3 steps: input → loading → result
# ============================================================================

if "step" not in st.session_state:
    st.session_state.step = "input"


# ============================================================================
# Constants used in both views
# ============================================================================

SEARCHBOX_STYLE = {
    "searchbox": {
        "control": {
            "backgroundColor": "#0B111C",
            "border": "1px solid #2A3344",
            "borderRadius": "8px",
            "minHeight": "44px",
            "boxShadow": "none",
        },
        "menu": {"backgroundColor": "#0B111C", "border": "1px solid #2A3344"},
        "menuList": {"backgroundColor": "#0B111C"},
        "option": {"backgroundColor": "#0B111C", "color": "#FFFFFF", "padding": "10px 12px"},
        "singleValue": {"color": "#FFFFFF"},
        "placeholder": {"color": "#9AA3B2"},
        "input": {"color": "#FFFFFF"},
    },
}

# === User context (hardcoded "connected vehicle" info) ===
VEHICLE_NAME = TESLA_M3_LR["name"]
VEHICLE_CURRENT_SOC = 67
VEHICLE_DEFAULT_STYLE = "Dynamique"
VEHICLE_LOCATION_LABEL = "52 Rue Laugier, 75017 Paris"
VEHICLE_LOCATION_COORDS = "48.886300,2.295000"


def render_input_view() -> None:
    """STEP 1 — Inputs page. Captures user choices, then transitions to loading."""
    _render_header()

    with st.expander("Mon trajet", expanded=True):
        use_vehicle_location = st.toggle(
            "Partir de l'emplacement actuel du véhicule",
            value=True,
            help=f"📍 {VEHICLE_LOCATION_LABEL}",
        )
        if use_vehicle_location:
            st.caption(f"📍 Départ : {VEHICLE_LOCATION_LABEL}")
            origin = VEHICLE_LOCATION_COORDS
        else:
            origin = st_searchbox(
                photon_search,
                key="origin",
                placeholder="Départ — ville, adresse, code postal",
                style_overrides=SEARCHBOX_STYLE,
            )
        destination = st_searchbox(
            photon_search,
            key="destination",
            placeholder="Arrivée — ville, adresse, code postal",
            style_overrides=SEARCHBOX_STYLE,
        )

    st.markdown(
        f"""
        <div style="background:#0B111C;border:1px solid #1A2030;border-radius:10px;
                    padding:1rem 1.1rem;margin:0.8rem 0;color:#FFFFFF;line-height:1.65;
                    font-size:0.95rem;">
          Votre véhicule est une <b style="color:#5FFFA7;">{VEHICLE_NAME}</b>.<br>
          Vous bénéficiez de l'option <b style="color:#5FFFA7;">véhicule connecté</b>.<br>
          Il est actuellement chargé à <b style="color:#5FFFA7;">{VEHICLE_CURRENT_SOC} %</b>.
        </div>
        """,
        unsafe_allow_html=True,
    )
    custom_soc = st.toggle("Simuler avec une autre charge ?", value=False)
    soc = st.slider("Charge pour la simulation (%)", 0, 100, VEHICLE_CURRENT_SOC) if custom_soc else VEHICLE_CURRENT_SOC

    st.markdown(
        f"""
        <div style="background:#0B111C;border:1px solid #1A2030;border-radius:10px;
                    padding:1rem 1.1rem;margin:0.8rem 0;color:#FFFFFF;line-height:1.65;
                    font-size:0.95rem;">
          Votre type de conduite est <b style="color:#5FFFA7;">{VEHICLE_DEFAULT_STYLE}</b>.
        </div>
        """,
        unsafe_allow_html=True,
    )
    custom_style = st.toggle("Paramétrer un autre style pour ce trajet ?", value=False)
    if custom_style:
        style_options = list(DRIVING_STYLES.keys())
        default_idx = style_options.index(VEHICLE_DEFAULT_STYLE) if VEHICLE_DEFAULT_STYLE in style_options else 1
        driving_style = st.radio(
            "Conduite", style_options, index=default_idx, horizontal=True,
        )
    else:
        driving_style = VEHICLE_DEFAULT_STYLE

    st.markdown("<div style='height:0.4rem'></div>", unsafe_allow_html=True)
    if st.button("Calculer mon trajet", type="primary"):
        st.session_state.inputs = {
            "origin": origin,
            "destination": destination,
            "soc": soc,
            "driving_style": driving_style,
        }
        st.session_state.pop("result_data", None)
        st.session_state.step = "loading"
        st.rerun()


# ============================================================================
# Result rendering
# ============================================================================

def render_trip_body(
    result: RouteResult,
    plan: TripPlan,
    origin,
    destination,
    meta: dict | None = None,
    key_suffix: str = "main",
) -> None:
    """Map + stops list only. Metrics + title are handled by the caller."""
    if not plan.feasible:
        st.error(f"Trajet non réalisable : {plan.reason}")

    # Map
    m = folium.Map(tiles="CartoDB dark_matter")
    pts = plan.updated_points
    all_lats, all_lngs = [], []
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        color = soc_color((a.soc_pct + b.soc_pct) / 2)
        folium.PolyLine(
            [(a.lat, a.lng), (b.lat, b.lng)],
            color=color, weight=5, opacity=0.85,
        ).add_to(m)
        all_lats.append(a.lat)
        all_lngs.append(a.lng)
    if pts:
        all_lats.append(pts[-1].lat)
        all_lngs.append(pts[-1].lng)
    folium.Marker(origin, tooltip="Départ", icon=folium.Icon(color="green")).add_to(m)
    folium.Marker(destination, tooltip="Arrivée", icon=folium.Icon(color="blue")).add_to(m)
    for i, s in enumerate(plan.stops):
        folium.Marker(
            location=[s.lat, s.lng],
            tooltip=(
                f"#{i+1} — {s.name}<br>"
                f"{s.power_kw:.0f} kW • {s.operator}<br>"
                f"{s.kwh_added:.0f} kWh • {fmt_duration(s.charge_time_min * 60)}"
            ),
            icon=folium.DivIcon(
                html=(
                    f'<div style="background:#5FFFA7;color:#03060D;border-radius:50%;'
                    f'width:30px;height:30px;display:flex;align-items:center;justify-content:center;'
                    f'font-weight:700;border:2px solid #03060D;'
                    f'box-shadow:0 2px 8px rgba(0,0,0,0.5);">{i+1}</div>'
                ),
                icon_size=(30, 30),
                icon_anchor=(15, 15),
            ),
        ).add_to(m)
    if all_lats and all_lngs:
        m.fit_bounds(
            [[min(all_lats), min(all_lngs)], [max(all_lats), max(all_lngs)]],
            padding=(20, 20),
        )
    st_folium(m, height=380, width=None, returned_objects=[], key=f"map_{key_suffix}")

    # Stops list (1 column, full width)
    if plan.stops:
        st.markdown(
            "<div style='font-weight:700;font-size:1.05rem;margin:0.8rem 0 0.5rem 0;'>"
            "Arrêts recharge</div>",
            unsafe_allow_html=True,
        )
        extras = meta.get("stops_extras", []) if meta else []
        for idx, s in enumerate(plan.stops):
            extra = extras[idx] if idx < len(extras) else {}
            avail = extra.get("availability") or {"label": "⚪ Inconnue"}
            cost = extra.get("cost") or {}
            city_line = s.city if s.city else "&nbsp;"
            avail_extra = ""
            if avail.get("n_total"):
                avail_extra = f" ({avail.get('n_available', 0)}/{avail['n_total']})"
            price_line = ""
            if cost:
                price_line = (
                    f'<div style="display:flex;justify-content:space-between;'
                    f'margin-top:0.4rem;font-size:0.9rem;">'
                    f'<span style="color:#9AA3B2;">{cost["price_per_kwh"]:.2f} €/kWh</span>'
                    f'<span style="color:#FFFFFF;font-weight:600;">'
                    f'{cost["total_eur"]:.1f} €</span></div>'
                )
            st.markdown(
                f"""
                <div style="background:#0B111C;border:1px solid #1A2030;border-radius:10px;
                            padding:0.9rem 1.1rem;margin-bottom:0.6rem;">
                  <div style="font-weight:700;color:#5FFFA7;font-size:1rem;
                              margin-bottom:0.35rem;">#{idx+1} — {s.name}</div>
                  <div style="color:#9AA3B2;font-size:0.82rem;line-height:1.5;">
                    {city_line} · {s.operator}<br>
                    {avail['label']}{avail_extra}
                  </div>
                  <div style="display:flex;justify-content:space-between;margin-top:0.55rem;
                              font-size:0.9rem;color:#FFFFFF;">
                    <span>km {s.km:.0f}</span><span>{s.power_kw:.0f} kW</span>
                  </div>
                  <div style="display:flex;justify-content:space-between;margin-top:0.35rem;
                              font-size:0.9rem;">
                    <span style="color:#9AA3B2;">
                      {s.soc_arrival_pct:.0f} % → {s.soc_leave_pct:.0f} %
                    </span>
                    <span style="color:#5FFFA7;font-weight:600;">
                      {fmt_duration(s.charge_time_min * 60)}
                    </span>
                  </div>
                  {price_line}
                </div>
                """,
                unsafe_allow_html=True,
            )
    else:
        st.caption("Aucun arrêt nécessaire.")


def render_trip(  # kept for backward compat — wraps render_trip_body
    result: RouteResult,
    plan: TripPlan,
    origin,
    destination,
    meta: dict | None = None,
    key_suffix: str = "main",
) -> None:
    n_stops = len(plan.stops)
    total_cost = (meta or {}).get("total_cost_eur", 0.0)

    # Metrics: 2x2 grid — fits on both mobile (~375px) and desktop (~700px centered).
    r1c1, r1c2 = st.columns(2)
    r1c1.metric("Distance", f"{result.total_km:.0f} km")
    r1c2.metric("Durée totale", fmt_duration(plan.total_time_s))
    r2c1, r2c2 = st.columns(2)
    r2c1.metric("Coût total", f"{total_cost:.0f} €")
    r2c2.metric("Arrêts", str(n_stops))

    bits = [
        f"conduite {fmt_duration(plan.drive_time_s)}",
        f"charges {fmt_duration(plan.charge_time_s)}",
    ]
    if meta:
        recharge = meta.get("recharge_cost_eur", 0.0)
        toll = meta.get("toll_eur", 0.0)
        if recharge or toll:
            bits.append(f"recharge {recharge:.0f} € + péage {toll:.0f} €")
        if meta.get("uses_notoll"):
            bits.append("trajet sans péage")
        if "avg_temp_c" in meta:
            bits.append(f"T° {meta['avg_temp_c']:.0f} °C")
        if "avg_wind_kmh" in meta:
            bits.append(f"vent {meta['avg_wind_kmh']:.0f} km/h")
        if "total_ascent_m" in meta:
            bits.append(f"dénivelé+ {meta['total_ascent_m']:.0f} m")
    bits.append(f"SoC arrivée {plan.arrival_soc_pct:.0f} %")
    st.caption(" • ".join(bits))

    if not plan.feasible:
        st.error(f"Trajet non réalisable : {plan.reason}")

    # Map
    m = folium.Map(tiles="CartoDB dark_matter")
    pts = plan.updated_points
    all_lats, all_lngs = [], []
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        color = soc_color((a.soc_pct + b.soc_pct) / 2)
        folium.PolyLine(
            [(a.lat, a.lng), (b.lat, b.lng)],
            color=color, weight=5, opacity=0.85,
        ).add_to(m)
        all_lats.append(a.lat)
        all_lngs.append(a.lng)
    if pts:
        all_lats.append(pts[-1].lat)
        all_lngs.append(pts[-1].lng)
    folium.Marker(origin, tooltip="Départ", icon=folium.Icon(color="green")).add_to(m)
    folium.Marker(destination, tooltip="Arrivée", icon=folium.Icon(color="blue")).add_to(m)
    for i, s in enumerate(plan.stops):
        folium.Marker(
            location=[s.lat, s.lng],
            tooltip=(
                f"#{i+1} — {s.name}<br>"
                f"{s.power_kw:.0f} kW • {s.operator}<br>"
                f"{s.kwh_added:.0f} kWh • {fmt_duration(s.charge_time_min * 60)}"
            ),
            icon=folium.DivIcon(
                html=(
                    f'<div style="background:#5FFFA7;color:#03060D;border-radius:50%;'
                    f'width:30px;height:30px;display:flex;align-items:center;justify-content:center;'
                    f'font-weight:700;border:2px solid #03060D;'
                    f'box-shadow:0 2px 8px rgba(0,0,0,0.5);">{i+1}</div>'
                ),
                icon_size=(30, 30),
                icon_anchor=(15, 15),
            ),
        ).add_to(m)
    if all_lats and all_lngs:
        m.fit_bounds(
            [[min(all_lats), min(all_lngs)], [max(all_lats), max(all_lngs)]],
            padding=(20, 20),
        )
    st_folium(m, height=420, width=None, returned_objects=[], key=f"map_{key_suffix}")

    # Stops — single column, one card per row.
    if plan.stops:
        st.markdown(
            "<div style='font-weight:700;font-size:1.05rem;margin:1rem 0 0.5rem 0;'>"
            "Arrêts recharge</div>",
            unsafe_allow_html=True,
        )
        extras = meta.get("stops_extras", []) if meta else []
        for idx, s in enumerate(plan.stops):
            extra = extras[idx] if idx < len(extras) else {}
            avail = extra.get("availability") or {"label": "⚪ Inconnue"}
            cost = extra.get("cost") or {}
            city_line = s.city if s.city else "&nbsp;"
            avail_extra = ""
            if avail.get("n_total"):
                avail_extra = f" ({avail.get('n_available', 0)}/{avail['n_total']})"
            price_line = ""
            if cost:
                price_line = (
                    f'<div style="display:flex;justify-content:space-between;'
                    f'margin-top:0.4rem;font-size:0.9rem;">'
                    f'<span style="color:#9AA3B2;">{cost["price_per_kwh"]:.2f} €/kWh</span>'
                    f'<span style="color:#FFFFFF;font-weight:600;">'
                    f'{cost["total_eur"]:.1f} €</span></div>'
                )
            st.markdown(
                f"""
                <div style="background:#0B111C;border:1px solid #1A2030;border-radius:10px;
                            padding:0.9rem 1.1rem;margin-bottom:0.6rem;">
                  <div style="font-weight:700;color:#5FFFA7;font-size:1rem;
                              margin-bottom:0.35rem;">#{idx+1} — {s.name}</div>
                  <div style="color:#9AA3B2;font-size:0.82rem;line-height:1.5;">
                    {city_line} · {s.operator}<br>
                    {avail['label']}{avail_extra}
                  </div>
                  <div style="display:flex;justify-content:space-between;margin-top:0.55rem;
                              font-size:0.9rem;color:#FFFFFF;">
                    <span>km {s.km:.0f}</span><span>{s.power_kw:.0f} kW</span>
                  </div>
                  <div style="display:flex;justify-content:space-between;margin-top:0.35rem;
                              font-size:0.9rem;">
                    <span style="color:#9AA3B2;">
                      {s.soc_arrival_pct:.0f} % → {s.soc_leave_pct:.0f} %
                    </span>
                    <span style="color:#5FFFA7;font-weight:600;">
                      {fmt_duration(s.charge_time_min * 60)}
                    </span>
                  </div>
                  {price_line}
                </div>
                """,
                unsafe_allow_html=True,
            )
    else:
        st.caption("Aucun arrêt nécessaire.")


# ============================================================================
# Pipeline: heavy computation. Returns a dict cached in session_state.
# ============================================================================

def compute_pipeline(inputs: dict) -> dict:
    """Run HERE + enrichment + planner + availability + cost for a set of inputs.
    Returns everything needed by render_result_view()."""
    here_key = get_secret("HERE_API_KEY")
    if not here_key:
        raise RuntimeError("Aucune clé HERE_API_KEY trouvée.")

    origin_coords = _parse_coords(inputs["origin"])
    destination_coords = _parse_coords(inputs["destination"])
    if not origin_coords or not destination_coords:
        raise ValueError("Départ ou arrivée manquant.")

    soc = inputs["soc"]
    driving_style = inputs["driving_style"]
    model = apply_driving_style(TESLA_M3_LR, driving_style)
    df_all = get_irve_cached()
    corridor_km = 5.0

    def pipeline(avoid_tolls: bool):
        result = fetch_route_here(origin_coords, destination_coords, soc, model, here_key, avoid_tolls=avoid_tolls)
        m: dict = {}
        result, m = enrich_route(result, model, use_weather=True, use_elevation=True)
        df_corridor = filter_corridor(df_all, result.points, corridor_km=corridor_km)
        df = filter_stations(df_corridor, categories=["Rapide", "HPC", "Ultra-rapide"])
        if not df.empty:
            df = df.copy()
            df["price_per_kwh"] = df.apply(
                lambda row: estimate_price_per_kwh(
                    row.get("nom_operateur"),
                    float(row.get("puissance_nominale") or 50.0),
                )[0],
                axis=1,
            )
        m["stations"] = df
        return result, m, df

    result_toll, meta_toll, df_toll = pipeline(avoid_tolls=False)
    result_notoll, meta_notoll, df_notoll = pipeline(avoid_tolls=True)

    # Compute all 4 (mode, toll) combinations so the result page can toggle freely.
    plan_fast_toll = plan_trip(result_toll, df_toll, model, initial_soc_pct=soc, mode="fast")
    plan_fast_notoll = plan_trip(result_notoll, df_notoll, model, initial_soc_pct=soc, mode="fast")

    def search_eco(result, df, time_budget_s):
        fallback = plan_trip(result, df, model, initial_soc_pct=soc, mode="eco", price_weight=40.0)
        for pw in (400.0, 250.0, 150.0, 80.0, 40.0):
            cand = plan_trip(result, df, model, initial_soc_pct=soc, mode="eco", price_weight=pw)
            if cand.feasible and cand.total_time_s <= time_budget_s:
                return cand
        return fallback

    plan_eco_toll = search_eco(result_toll, df_toll, plan_fast_toll.total_time_s * 1.20)
    plan_eco_notoll = search_eco(result_notoll, df_notoll, plan_fast_notoll.total_time_s * 1.20)

    tomtom_key = get_secret("TOMTOM_API_KEY")

    def enrich_stops(plan, df_src):
        extras, total = [], 0.0
        for s in plan.stops:
            avail = fetch_availability(s.lat, s.lng, s.name, tomtom_key or "")
            tarif_text = None
            if not df_src.empty and "tarification" in df_src.columns:
                near = df_src[
                    (df_src["lat"].sub(s.lat).abs() < 1e-4)
                    & (df_src["lng"].sub(s.lng).abs() < 1e-4)
                ]
                if not near.empty:
                    tarif_text = near.iloc[0].get("tarification")
            cost = estimate_stop_cost(s.operator, s.power_kw, s.kwh_added, tarif_text)
            extras.append({"availability": avail, "cost": cost})
            total += cost["total_eur"]
        return extras, total

    extras_fast_toll, rc_fast_toll = enrich_stops(plan_fast_toll, df_toll)
    extras_fast_notoll, rc_fast_notoll = enrich_stops(plan_fast_notoll, df_notoll)
    extras_eco_toll, rc_eco_toll = enrich_stops(plan_eco_toll, df_toll)
    extras_eco_notoll, rc_eco_notoll = enrich_stops(plan_eco_notoll, df_notoll)

    return {
        "origin": origin_coords,
        "destination": destination_coords,
        "results": {"toll": result_toll, "notoll": result_notoll},
        "metas": {"toll": meta_toll, "notoll": meta_notoll},
        "combos": {
            ("fast", "toll"): {"plan": plan_fast_toll, "extras": extras_fast_toll, "recharge": rc_fast_toll},
            ("fast", "notoll"): {"plan": plan_fast_notoll, "extras": extras_fast_notoll, "recharge": rc_fast_notoll},
            ("eco", "toll"): {"plan": plan_eco_toll, "extras": extras_eco_toll, "recharge": rc_eco_toll},
            ("eco", "notoll"): {"plan": plan_eco_notoll, "extras": extras_eco_notoll, "recharge": rc_eco_notoll},
        },
    }


# ============================================================================
# Loading view (STEP 2)
# ============================================================================

def render_loading_view() -> None:
    # Centered loading screen with a big custom spinner. Inputs from the previous
    # step are not rendered because the dispatcher routes here exclusively.
    import base64
    logo_b64 = ""
    try:
        with open("assets/logo-qivia.png", "rb") as fp:
            logo_b64 = base64.b64encode(fp.read()).decode("ascii")
    except Exception:
        pass
    st.markdown(
        f"""
        <style>
        /* Fullscreen overlay — covers anything from the previous step
           that might still be in the DOM during transition. */
        .qivia-loading {{
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            width: 100vw;
            height: 100vh;
            background: #03060D;
            z-index: 2147483647;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            text-align: center;
            padding: 1rem;
        }}
        .qivia-loading img {{
            width: 200px;
            height: auto;
            margin-bottom: 2rem;
        }}
        .qivia-loading h2 {{
            color: #5FFFA7 !important;
            margin: 0 0 0.5rem 0;
            font-size: 1.8rem;
            font-family: 'Plus Jakarta Sans', -apple-system, sans-serif;
            font-weight: 700;
        }}
        .qivia-loading p {{
            color: #9AA3B2;
            font-size: 0.95rem;
            margin: 0 0 2rem 0;
            font-family: 'Plus Jakarta Sans', -apple-system, sans-serif;
        }}
        .qivia-spinner {{
            width: 90px;
            height: 90px;
            border: 6px solid rgba(95, 255, 167, 0.15);
            border-top-color: #5FFFA7;
            border-radius: 50%;
            animation: qspin 1s linear infinite;
        }}
        @keyframes qspin {{
            to {{ transform: rotate(360deg); }}
        }}
        </style>
        <div class="qivia-loading">
          {"<img src='data:image/png;base64," + logo_b64 + "' alt='Qivia' />" if logo_b64 else ""}
          <h2>Calcul en cours…</h2>
          <p>Itinéraire, météo, dénivelé, bornes, prix</p>
          <div class="qivia-spinner"></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    try:
        st.session_state.result_data = compute_pipeline(st.session_state.inputs)
        st.session_state.step = "result"
    except Exception as e:
        st.session_state.error = str(e)
        st.session_state.step = "error"
    st.rerun()


# ============================================================================
# Result view (STEP 3)
# ============================================================================

def render_result_view() -> None:
    data = st.session_state.result_data

    # Compact title row: back button + personalized title.
    col_back, col_title = st.columns([2, 7])
    with col_back:
        if st.button("← Nouveau", key="back_btn"):
            st.session_state.step = "input"
            st.session_state.pop("result_data", None)
            st.rerun()
    with col_title:
        st.markdown(
            '<h2 style="margin:0.4rem 0 0 0;font-size:1.25rem;color:#FFFFFF;line-height:1.2;">'
            'Voilà votre trajet <span class="qivia-highlight">Arthur</span>'
            '</h2>',
            unsafe_allow_html=True,
        )

    # Two toggles in one row — pick the strategy + the toll option.
    t1, t2 = st.columns(2)
    with t1:
        mode_choice = st.radio(
            "Stratégie", ["Rapide", "Économique"],
            horizontal=True, key="mode_toggle", label_visibility="collapsed",
        )
    with t2:
        toll_choice = st.radio(
            "Péage", ["Avec péage", "Sans péage"],
            horizontal=True, key="toll_toggle", label_visibility="collapsed",
        )
    mode_key = "fast" if mode_choice == "Rapide" else "eco"
    toll_key = "toll" if toll_choice == "Avec péage" else "notoll"

    combo = data["combos"][(mode_key, toll_key)]
    plan = combo["plan"]
    extras = combo["extras"]
    recharge_cost = combo["recharge"]
    result = data["results"][toll_key]
    meta_base = data["metas"][toll_key]
    toll_cost = result.total_toll_eur
    n_stops = len(plan.stops)

    # Compact 5-metric row that fits on a single mobile line.
    st.markdown(
        """
        <style>
        .result-metrics {
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 0.4rem;
            margin: 0.7rem 0 0.6rem 0;
        }
        .result-metric {
            background: #0B111C;
            border: 1px solid #1A2030;
            border-radius: 8px;
            padding: 0.5rem 0.4rem;
            text-align: center;
        }
        .result-metric .lbl {
            color: #9AA3B2;
            font-size: 0.65rem;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .result-metric .val {
            color: #5FFFA7;
            font-size: 1.05rem;
            font-weight: 700;
            margin-top: 0.2rem;
            white-space: nowrap;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        f"""
        <div class="result-metrics">
          <div class="result-metric"><div class="lbl">Distance</div><div class="val">{result.total_km:.0f} km</div></div>
          <div class="result-metric"><div class="lbl">Durée</div><div class="val">{fmt_duration(plan.total_time_s)}</div></div>
          <div class="result-metric"><div class="lbl">Recharge</div><div class="val">{recharge_cost:.0f} €</div></div>
          <div class="result-metric"><div class="lbl">Péage</div><div class="val">{toll_cost:.0f} €</div></div>
          <div class="result-metric"><div class="lbl">Arrêts</div><div class="val">{n_stops}</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Map + stops.
    meta = {
        **meta_base,
        "stops_extras": extras,
        "recharge_cost_eur": recharge_cost,
        "toll_eur": toll_cost,
        "total_cost_eur": recharge_cost + toll_cost,
    }
    render_trip_body(result, plan, data["origin"], data["destination"], meta, f"{mode_key}_{toll_key}")


# ============================================================================
# Error view
# ============================================================================

def render_error_view() -> None:
    _render_header()
    st.error(f"Erreur lors du calcul : {st.session_state.get('error', 'inconnue')}")
    if st.button("← Retour au formulaire"):
        st.session_state.step = "input"
        st.session_state.pop("error", None)
        st.rerun()


# ============================================================================
# Wizard dispatcher
# ============================================================================

step = st.session_state.get("step", "input")
if step == "input":
    render_input_view()
elif step == "loading":
    render_loading_view()
elif step == "result":
    render_result_view()
elif step == "error":
    render_error_view()
