"""Streamlit app: HERE EV routing + charging stops planner.

Mobile-first layout: single centered column, expanders for inputs, no sidebar.
"""
from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import folium
import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from streamlit_folium import st_folium
from streamlit_js_eval import get_geolocation
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
        # Keep only French addresses.
        country = (props.get("country") or "").strip().lower()
        if country and country not in ("france", "frankreich", "frança"):
            continue
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


@st.cache_data(ttl=600, show_spinner=False)
def _reverse_geocode(lat: float, lng: float) -> str:
    """Convert coordinates to a clean human-readable address via Photon."""
    try:
        r = requests.get(
            "https://photon.komoot.io/reverse",
            params={"lat": lat, "lon": lng, "lang": "fr"},
            timeout=5,
            headers={"User-Agent": "ev-route-planner/0.1"},
        )
        r.raise_for_status()
        features = r.json().get("features", [])
        if not features:
            return f"{lat:.4f}, {lng:.4f}"
        props = features[0].get("properties", {})
        housenumber = (props.get("housenumber") or "").strip()
        street = (props.get("street") or "").strip()
        name = (props.get("name") or "").strip()
        city = (props.get("city") or "").strip()
        postcode = (props.get("postcode") or "").strip()
        if ";" in postcode:
            postcode = ""
        if housenumber and street:
            primary = f"{housenumber} {street}"
        elif street:
            primary = street
        else:
            primary = name or ""
        parts: list[str] = []
        if primary:
            parts.append(primary)
        loc_bits: list[str] = []
        if postcode:
            loc_bits.append(postcode)
        if city and city != primary:
            loc_bits.append(city)
        if loc_bits:
            parts.append(" ".join(loc_bits))
        return ", ".join(parts) if parts else f"{lat:.4f}, {lng:.4f}"
    except Exception:
        return f"{lat:.4f}, {lng:.4f}"


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
    /* Push content below Safari's top URL bar on iOS (and equivalent on Chrome Android). */
    [data-testid="stMain"] > div:first-child,
    .main .block-container {
        padding-top: max(2.5rem, env(safe-area-inset-top, 0px)) !important;
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

    /* Keep horizontal blocks (st.columns) horizontal even on mobile — needed
       for the "address + ⋮ menu" row to stay inline. */
    [data-testid="stHorizontalBlock"] {
        flex-direction: row !important;
        flex-wrap: nowrap !important;
        gap: 0 !important;
    }
    [data-testid="stHorizontalBlock"] > [data-testid="column"] {
        min-width: 0 !important;
    }

    /* Keep the native searchbox chevron — but hide the vertical separator. */
    [class*="-IndicatorSeparator"] {
        display: none !important;
    }

    /* The départ origin button — styled to look like a cartouche/input,
       with left-aligned text and chevron at the end. */
    [data-testid="stExpanderDetails"] [data-testid="stButton"]:first-of-type button {
        background: #0B111C !important;
        border: 1px solid #2A3344 !important;
        border-radius: 8px !important;
        color: #FFFFFF !important;
        text-align: left !important;
        padding: 0.7rem 0.9rem !important;
        min-height: 44px !important;
        font-weight: 400 !important;
        justify-content: flex-start !important;
        white-space: normal !important;
        line-height: 1.3 !important;
    }
    [data-testid="stExpanderDetails"] [data-testid="stButton"]:first-of-type button:hover {
        background: #0F1622 !important;
        border-color: #5FFFA7 !important;
        color: #FFFFFF !important;
    }
    [data-testid="stExpanderDetails"] [data-testid="stButton"]:first-of-type button p {
        margin: 0 !important;
        font-size: 0.95rem !important;
    }


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

    /* Borderless "⋯" button used next to the départ field.
       Aggressive selectors to win Streamlit's emotion CSS specificity. */
    [class*="st-key-origin_more"] button,
    [class*="st-key-origin_more"] [data-testid^="stBaseButton"] {
        background: transparent !important;
        background-color: transparent !important;
        border: 0 !important;
        border-color: transparent !important;
        box-shadow: none !important;
        outline: none !important;
        color: #9AA3B2 !important;
        font-size: 1.6rem !important;
        line-height: 1 !important;
        padding: 0.85rem 0 !important;  /* vertically aligns ⋯ with input text */
        min-height: 0 !important;
    }
    [class*="st-key-origin_more"] button:hover,
    [class*="st-key-origin_more"] button:focus,
    [class*="st-key-origin_more"] button:active {
        color: #5FFFA7 !important;
        background: transparent !important;
        background-color: transparent !important;
        border: 0 !important;
        box-shadow: none !important;
    }
    /* Cartouche-button (gps / car) — fill the column, left-aligned text.
       Streamlit nests button > div > div > p, so we force alignment at every level. */
    [class*="st-key-origin_box"] button,
    [class*="st-key-origin_box"] button > div,
    [class*="st-key-origin_box"] button > div > div,
    [class*="st-key-origin_box"] button p {
        text-align: left !important;
        justify-content: flex-start !important;
        width: 100% !important;
    }

    /* Force the départ / arrivée columns to respect a 75/25 ratio — the
       widgets inside (searchbox, button) were ignoring Streamlit's column
       sizing and expanding past their column. We target the parent column
       that contains a specific widget via :has(). */
    [data-testid="stColumn"]:has([class*="st-key-origin_box"]),
    [data-testid="stColumn"]:has([class*="st-key-origin_typed"]),
    [data-testid="stColumn"]:has([class*="st-key-destination"]) {
        flex: 0 0 75% !important;
        max-width: 75% !important;
        width: 75% !important;
    }
    [data-testid="stColumn"]:has([class*="st-key-origin_more"]) {
        flex: 0 0 22% !important;
        max-width: 22% !important;
        width: 22% !important;
    }

    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================================
# Password gate + header
# ============================================================================

def _logo_b64() -> str:
    """Read the Qivia logo and return it as a base64 string (empty on failure)."""
    import base64
    try:
        path = Path(__file__).parent / "assets" / "logo-qivia.png"
        return base64.b64encode(path.read_bytes()).decode("ascii")
    except Exception:
        return ""


def _render_header() -> None:
    # Inline base64 image — more robust than st.image on Streamlit Cloud
    # (no broken-image fallback if the asset path resolution hiccups).
    b64 = _logo_b64()
    img_html = (
        f'<img src="data:image/png;base64,{b64}" '
        f'style="width:80px;height:auto;display:block;margin:0;" alt="Qivia">'
        if b64 else ""
    )
    st.markdown(
        f'{img_html}'
        '<h1 style="margin:0.6rem 0 0 0;font-size:1.6rem;line-height:1.2;">'
        'Bonjour <span class="qivia-highlight">Arthur</span>, où va-t-on ?'
        '</h1>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="qivia-accent"></div>', unsafe_allow_html=True)


def _check_password() -> None:
    # Streamlit's session_state can be lost on cold start / iOS tab sleep.
    # Persist the auth marker in the URL query so it survives those resets.
    if "auth" in st.query_params:
        st.session_state["auth_ok"] = True
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
            # Persist via URL so the auth survives Streamlit session resets
            # (cold start, iOS tab sleep, mobile network blip, etc.).
            st.query_params["auth"] = "ok"
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
        # Keep the dropdown chevron — it's the natural way to open the
        # options list (Votre position / Votre voiture / Saisir adresse).
        "dropdownIndicator": {"color": "#9AA3B2"},
        "indicatorSeparator": {"display": "none"},
    },
}

# === User context (hardcoded "connected vehicle" info) ===
VEHICLE_NAME = TESLA_M3_LR["name"]
VEHICLE_CURRENT_SOC = 67
VEHICLE_DEFAULT_STYLE = "Dynamique"
VEHICLE_LOCATION_LABEL = "52 rue de Picpus, 75012 Paris"
VEHICLE_LOCATION_COORDS = "48.846800,2.394500"


@st.dialog("Choisir le départ", width="small")
def _origin_dialog() -> None:
    """Modal with the 3 options. Picking 'Saisir une adresse' switches the
    main view into searchbox mode; the typing happens outside the dialog."""
    if st.session_state.get("geoloc_coords"):
        geo = st.session_state.get("geoloc_label", "…")
        if st.button(f"📍 Votre position ({geo})", key="dlg_gps",
                     use_container_width=True):
            st.session_state.origin_mode = "gps"
            st.rerun()
    if st.button(f"🚗 Votre voiture ({VEHICLE_LOCATION_LABEL})",
                 key="dlg_car", use_container_width=True):
        st.session_state.origin_mode = "car"
        st.rerun()
    if st.button("✏️ Saisir une adresse", key="dlg_type",
                 use_container_width=True):
        st.session_state.origin_mode = "type"
        st.rerun()


def render_input_view() -> None:
    """STEP 1 — Inputs page. Captures user choices, then transitions to loading."""
    _render_header()

    # Silent auto-geolocation. get_geolocation() is asynchronous: first
    # render returns None while the browser prompt is up; once the user
    # grants permission, the JS resolves and Streamlit reruns.
    if "geoloc_coords" not in st.session_state:
        loc = get_geolocation()
        if loc and loc.get("coords"):
            lat = float(loc["coords"]["latitude"])
            lng = float(loc["coords"]["longitude"])
            st.session_state.geoloc_coords = f"{lat:.6f},{lng:.6f}"
            st.session_state.geoloc_label = _reverse_geocode(lat, lng)

    # Default mode: GPS if available, else vehicle.
    if "origin_mode" not in st.session_state:
        st.session_state.origin_mode = (
            "gps" if "geoloc_coords" in st.session_state else "car"
        )

    mode = st.session_state.origin_mode

    # DÉPART — left column (box) + right column (borderless ⋯).
    # vertical_alignment="top" so the ⋯ stays anchored to the input row even
    # when the searchbox dropdown expands the left column's height.
    col_main, col_more = st.columns([1, 5], vertical_alignment="top")
    with col_main:
        if mode == "type":
            typed = st_searchbox(
                photon_search,
                key="origin_typed",
                placeholder="Saisir une adresse de départ",
                style_overrides=SEARCHBOX_STYLE,
            )
            if typed:
                st.session_state.typed_origin_coords = typed
            origin = st.session_state.get("typed_origin_coords")
        else:
            if mode == "gps":
                geo = st.session_state.get("geoloc_label", "Détection en cours…")
                display = f"📍 {geo}"
                origin = st.session_state.get("geoloc_coords") or VEHICLE_LOCATION_COORDS
            else:  # car
                display = f"🚗 {VEHICLE_LOCATION_LABEL}"
                origin = VEHICLE_LOCATION_COORDS
            # Cartouche-button (no chevron, left-aligned text via CSS).
            if st.button(display, key="origin_box",
                         use_container_width=True):
                _origin_dialog()
    with col_more:
        if st.button("⋯", key="origin_more",
                     help="Changer la source du départ"):
            _origin_dialog()

    # ARRIVÉE — same split for visual width parity with départ; right col empty.
    col_arr, col_arr_pad = st.columns([5, 1.5], vertical_alignment="top")
    with col_arr:
        destination = st_searchbox(
            photon_search,
            key="destination",
            placeholder="Arrivée",
            style_overrides=SEARCHBOX_STYLE,
        )
    with col_arr_pad:
        st.empty()

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

    # Map. Downsample the polyline to ~80 segments so folium HTML stays small
    # and the iframe initializes in <500ms (vs ~3-4s on full resolution).
    m = folium.Map(tiles="CartoDB dark_matter")
    # Hide attribution + style the Leaflet popup wrapper to match the dark theme.
    m.get_root().html.add_child(folium.Element(
        """
        <style>
          .leaflet-control-attribution { display: none !important; }
          .leaflet-popup-content-wrapper, .leaflet-popup-tip {
            background: #0B111C !important;
            color: #FFFFFF !important;
            border: 1px solid #1A2030 !important;
            box-shadow: 0 4px 14px rgba(0,0,0,0.5) !important;
          }
          .leaflet-popup-content {
            margin: 0.7rem 0.8rem !important;
            font-family: 'Plus Jakarta Sans', -apple-system, sans-serif !important;
          }
          .leaflet-popup-close-button {
            color: #5FFFA7 !important;
            font-size: 1.3rem !important;
            padding: 6px !important;
          }
        </style>
        """
    ))
    pts_full = plan.updated_points
    TARGET_SEGMENTS = 80
    step = max(1, len(pts_full) // TARGET_SEGMENTS)
    pts = pts_full[::step]
    if pts and pts[-1] is not pts_full[-1]:
        pts.append(pts_full[-1])
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

    extras = meta.get("stops_extras", []) if meta else []
    for i, s in enumerate(plan.stops):
        extra = extras[i] if i < len(extras) else {}
        avail = extra.get("availability") or {"label": "⚪ Inconnue"}
        cost = extra.get("cost") or {}
        city_line = s.city if s.city else ""
        avail_extra = (
            f" ({avail.get('n_available', 0)}/{avail['n_total']})"
            if avail.get("n_total") else ""
        )
        price_line = ""
        if cost:
            price_line = (
                f'<div style="display:flex;justify-content:space-between;'
                f'margin-top:0.3rem;font-size:0.82rem;">'
                f'<span style="color:#9AA3B2;">{cost["price_per_kwh"]:.2f} €/kWh</span>'
                f'<span style="color:#FFFFFF;font-weight:600;">{cost["total_eur"]:.1f} €</span></div>'
            )
        popup_html = (
            f'<div style="min-width:200px;max-width:240px;color:#FFFFFF;'
            f'font-family:Plus Jakarta Sans,-apple-system,sans-serif;">'
            f'  <div style="font-weight:700;color:#5FFFA7;font-size:0.95rem;'
            f'              margin-bottom:0.3rem;line-height:1.2;">#{i+1} — {s.name}</div>'
            f'  <div style="color:#9AA3B2;font-size:0.78rem;line-height:1.5;">'
            f'    {city_line}{"<br>" if city_line else ""}{s.operator}<br>'
            f'    {avail["label"]}{avail_extra}'
            f'  </div>'
            f'  <div style="display:flex;justify-content:space-between;margin-top:0.45rem;'
            f'              font-size:0.85rem;color:#FFFFFF;">'
            f'    <span>km {s.km:.0f}</span><span>{s.power_kw:.0f} kW</span>'
            f'  </div>'
            f'  <div style="display:flex;justify-content:space-between;margin-top:0.3rem;'
            f'              font-size:0.85rem;">'
            f'    <span style="color:#9AA3B2;">{s.soc_arrival_pct:.0f}% → {s.soc_leave_pct:.0f}%</span>'
            f'    <span style="color:#5FFFA7;font-weight:600;">{fmt_duration(s.charge_time_min * 60)}</span>'
            f'  </div>'
            f'  {price_line}'
            f'</div>'
        )
        folium.Marker(
            location=[s.lat, s.lng],
            tooltip=f"#{i+1} — {s.name}",
            popup=folium.Popup(popup_html, max_width=260),
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
    # Key includes the plan's signature so any change in route/stops triggers
    # a new st_folium widget (instead of reusing a cached iframe).
    content_sig = f"{result.total_km:.0f}_{plan.total_time_s:.0f}_{len(plan.stops)}_{key_suffix}"
    st_folium(m, height=420, width=None, returned_objects=[], key=f"map_{content_sig}")


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

    # Map. Downsample the polyline to ~80 segments so folium HTML stays small
    # and the iframe initializes in <500ms (vs ~3-4s on full resolution).
    m = folium.Map(tiles="CartoDB dark_matter")
    # Hide the Leaflet / OpenStreetMap / CARTO attribution bar inside the map iframe.
    # (Personal use only — OSM/Carto require attribution for redistribution.)
    m.get_root().html.add_child(folium.Element(
        "<style>.leaflet-control-attribution { display: none !important; }</style>"
    ))
    pts_full = plan.updated_points
    TARGET_SEGMENTS = 80
    step = max(1, len(pts_full) // TARGET_SEGMENTS)
    pts = pts_full[::step]
    if pts and pts[-1] is not pts_full[-1]:
        pts.append(pts_full[-1])
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

def _compute_route_base(
    origin_coords, destination_coords, soc, model, df_all, avoid_tolls,
) -> dict:
    """Phase 1 (slow): HERE call + enrichment + station filtering. Shared
    across the two plan modes (fast / eco) of the same toll variant."""
    here_key = get_secret("HERE_API_KEY")
    result = fetch_route_here(
        origin_coords, destination_coords, soc, model, here_key, avoid_tolls=avoid_tolls,
    )
    # Enrichment (weather+elevation) and station filtering are independent — run
    # in parallel since they both consume the route points.
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_enrich = ex.submit(enrich_route, result, model, True, True)
        f_filter = ex.submit(filter_corridor, df_all, result.points, 5.0)
        result, meta = f_enrich.result()
        df_corridor = f_filter.result()

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
    return {"result": result, "meta": meta, "df": df}


def _compute_one_plan(base: dict, soc, model, mode: str, fast_plan=None) -> dict:
    """Phase 2 (fast): plan_trip + TomTom availability + cost for a single mode."""
    result = base["result"]
    df = base["df"]
    tomtom_key = get_secret("TOMTOM_API_KEY")

    if mode == "fast":
        plan = plan_trip(result, df, model, initial_soc_pct=soc, mode="fast")
    else:
        fast_for_budget = fast_plan or plan_trip(result, df, model, initial_soc_pct=soc, mode="fast")
        budget = fast_for_budget.total_time_s * 1.20
        plan = plan_trip(result, df, model, initial_soc_pct=soc, mode="eco", price_weight=40.0)
        for pw in (400.0, 250.0, 150.0, 80.0, 40.0):
            cand = plan_trip(result, df, model, initial_soc_pct=soc, mode="eco", price_weight=pw)
            if cand.feasible and cand.total_time_s <= budget:
                plan = cand
                break

    if not plan.stops:
        return {"plan": plan, "extras": [], "recharge": 0.0}

    def _avail(s):
        return fetch_availability(s.lat, s.lng, s.name, tomtom_key or "")

    with ThreadPoolExecutor(max_workers=min(8, len(plan.stops))) as ex:
        avails = list(ex.map(_avail, plan.stops))

    extras = []
    for s, avail in zip(plan.stops, avails):
        tarif_text = None
        if not df.empty and "tarification" in df.columns:
            near = df[
                (df["lat"].sub(s.lat).abs() < 1e-4)
                & (df["lng"].sub(s.lng).abs() < 1e-4)
            ]
            if not near.empty:
                tarif_text = near.iloc[0].get("tarification")
        cost = estimate_stop_cost(s.operator, s.power_kw, s.kwh_added, tarif_text)
        extras.append({"availability": avail, "cost": cost})
    return {"plan": plan, "extras": extras, "recharge": sum(e["cost"]["total_eur"] for e in extras)}


def _compute_variant_full(
    origin_coords, destination_coords, soc, model, df_all, avoid_tolls,
) -> dict:
    """Compute the FULL variant (both modes) — used by background threads."""
    base = _compute_route_base(
        origin_coords, destination_coords, soc, model, df_all, avoid_tolls,
    )
    fast_data = _compute_one_plan(base, soc, model, "fast")
    eco_data = _compute_one_plan(base, soc, model, "eco", fast_plan=fast_data["plan"])
    return {
        "result": base["result"],
        "meta": base["meta"],
        "_base": base,
        "plans": {"fast": fast_data["plan"], "eco": eco_data["plan"]},
        "extras": {"fast": fast_data["extras"], "eco": eco_data["extras"]},
        "recharges": {"fast": fast_data["recharge"], "eco": eco_data["recharge"]},
    }


def compute_pipeline(inputs: dict) -> dict:
    """Initial pipeline: only computes fast/toll (the most common combo).
    The other 3 combos (eco/toll, fast/notoll, eco/notoll) are computed by
    background threads as soon as the result page is displayed."""
    here_key = get_secret("HERE_API_KEY")
    if not here_key:
        raise RuntimeError("Aucune clé HERE_API_KEY trouvée.")

    origin_coords = _parse_coords(inputs["origin"])
    destination_coords = _parse_coords(inputs["destination"])
    if not origin_coords or not destination_coords:
        raise ValueError("Départ ou arrivée manquant.")

    model = apply_driving_style(TESLA_M3_LR, inputs["driving_style"])
    df_all = get_irve_cached()
    soc = inputs["soc"]

    # Compute the toll base (HERE + enrich + stations) and the fast plan only.
    base_toll = _compute_route_base(
        origin_coords, destination_coords, soc, model, df_all, avoid_tolls=False,
    )
    fast_toll = _compute_one_plan(base_toll, soc, model, "fast")

    return {
        "origin": origin_coords,
        "destination": destination_coords,
        "soc": soc,
        "model": model,
        "variants": {
            "toll": {
                "result": base_toll["result"],
                "meta": base_toll["meta"],
                "_base": base_toll,  # reused by background eco_toll computation
                "plans": {"fast": fast_toll["plan"]},
                "extras": {"fast": fast_toll["extras"]},
                "recharges": {"fast": fast_toll["recharge"]},
            }
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

    # As soon as the result view loads, kick off background computations for
    # the 3 remaining combos so they're ready when the user toggles.
    if "bg_executor" not in st.session_state:
        executor = ThreadPoolExecutor(max_workers=2)
        st.session_state.bg_executor = executor
        df_all = get_irve_cached()
        toll_base = data["variants"]["toll"].get("_base")
        # Thread 1: eco for toll variant — reuses HERE+enrich already done.
        if toll_base:
            st.session_state.bg_eco_toll = executor.submit(
                _compute_one_plan, toll_base, data["soc"], data["model"], "eco",
                data["variants"]["toll"]["plans"]["fast"],
            )
        # Thread 2: full no-toll variant (HERE + enrich + fast + eco).
        st.session_state.bg_notoll = executor.submit(
            _compute_variant_full,
            data["origin"], data["destination"],
            data["soc"], data["model"], df_all, True,
        )

    def _ensure_eco_toll() -> None:
        """If toll/eco not yet computed, wait on the background thread."""
        if "eco" in data["variants"]["toll"]["plans"]:
            return
        future = st.session_state.get("bg_eco_toll")
        if future is None:
            # Fallback: compute synchronously
            toll_base = data["variants"]["toll"].get("_base")
            eco_data = _compute_one_plan(
                toll_base, data["soc"], data["model"], "eco",
                data["variants"]["toll"]["plans"]["fast"],
            )
        else:
            spin_text = "Finalisation du mode économique…" if not future.done() else None
            if spin_text:
                with st.spinner(spin_text):
                    eco_data = future.result(timeout=30)
            else:
                eco_data = future.result()
            st.session_state.pop("bg_eco_toll", None)
        data["variants"]["toll"]["plans"]["eco"] = eco_data["plan"]
        data["variants"]["toll"]["extras"]["eco"] = eco_data["extras"]
        data["variants"]["toll"]["recharges"]["eco"] = eco_data["recharge"]
        st.session_state.result_data = data

    def _ensure_notoll() -> None:
        """If notoll variant not yet computed, wait on the background thread."""
        if "notoll" in data["variants"]:
            return
        future = st.session_state.get("bg_notoll")
        if future is None:
            df_all = get_irve_cached()
            data["variants"]["notoll"] = _compute_variant_full(
                data["origin"], data["destination"],
                data["soc"], data["model"], df_all, True,
            )
        else:
            spin_text = "Finalisation du trajet sans péage…" if not future.done() else None
            if spin_text:
                with st.spinner(spin_text):
                    data["variants"]["notoll"] = future.result(timeout=60)
            else:
                data["variants"]["notoll"] = future.result()
            st.session_state.pop("bg_notoll", None)
        st.session_state.result_data = data

    def _back_to_input():
        st.session_state.step = "input"
        st.session_state.pop("result_data", None)
        st.session_state.pop("bg_eco_toll", None)
        st.session_state.pop("bg_notoll", None)
        executor = st.session_state.pop("bg_executor", None)
        if executor:
            executor.shutdown(wait=False)

    # Title only (back button moved to the bottom under the map, where it's
    # not hidden behind Safari's URL bar on iOS).
    st.markdown(
        '<h2 style="margin:0.4rem 0 0 0;font-size:1.4rem;color:#FFFFFF;line-height:1.2;">'
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

    # Ensure the selected combo has been computed (waits on background threads
    # if needed). Initial fast/toll is always present from compute_pipeline.
    try:
        if toll_key == "notoll":
            _ensure_notoll()
        if mode_key == "eco" and toll_key == "toll":
            _ensure_eco_toll()
        # For eco/notoll: _ensure_notoll already brings in both plans.
    except Exception as e:
        st.warning(f"Calcul indisponible : {e}")
        st.session_state.mode_toggle = "Rapide"
        st.session_state.toll_toggle = "Avec péage"
        st.rerun()

    variant = data["variants"][toll_key]
    plan = variant["plans"][mode_key]
    extras = variant["extras"][mode_key]
    recharge_cost = variant["recharges"][mode_key]
    result = variant["result"]
    meta_base = variant["meta"]
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

    # Big primary button after the map: easy way to plan another trip.
    st.markdown("<div style='height:0.8rem'></div>", unsafe_allow_html=True)
    if st.button("Calculer un autre trajet", type="primary", key="back_btn_bottom",
                 use_container_width=True):
        _back_to_input()
        st.rerun()


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
