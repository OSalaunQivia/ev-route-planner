"""Streamlit app: HERE EV routing + charging stops planner."""
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
    power_color,
)


@st.cache_data(ttl=86_400, show_spinner="Téléchargement IRVE (~30 Mo, une seule fois)…")
def get_irve_cached():
    return load_irve()


@st.cache_data(ttl=300, show_spinner=False)
def photon_search(query: str) -> list[tuple[str, str]]:
    """Address/city/postcode autocomplete via Photon (free, OSM-based)."""
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
        # Surface the error so we can see it in the UI/terminal.
        print(f"[photon_search] erreur pour query={query!r}: {e}")
        st.warning(f"Recherche indisponible : {e}")
        return []
    results: list[tuple[str, str]] = []
    for f in data.get("features", []):
        props = f.get("properties", {})
        lng, lat = f["geometry"]["coordinates"]
        parts: list[str] = []
        if props.get("name"):
            parts.append(props["name"])
        loc = []
        if props.get("postcode"):
            loc.append(props["postcode"])
        if props.get("city") and props.get("city") != props.get("name"):
            loc.append(props["city"])
        if loc:
            parts.append(" ".join(loc))
        if props.get("state") and props.get("state") not in parts:
            parts.append(props["state"])
        if props.get("country"):
            parts.append(props["country"])
        label = ", ".join(parts) if parts else f"{lat:.3f}, {lng:.3f}"
        # Encode coords in the value as a string so streamlit-searchbox can serialize it.
        value = f"{lat:.6f},{lng:.6f}"
        results.append((label, value))
    if not results:
        print(f"[photon_search] aucun résultat pour query={query!r}, raw={data}")
    return results


def _parse_coords(value: str | None) -> tuple[float, float] | None:
    if not value or "," not in value:
        return None
    try:
        a, b = value.split(",", 1)
        return float(a), float(b)
    except ValueError:
        return None

load_dotenv(override=True)


def get_secret(name: str, default: str = "") -> str:
    """Read from Streamlit Cloud secrets first, fall back to env (local .env)."""
    try:
        val = st.secrets.get(name)
        if val:
            return str(val)
    except Exception:
        pass
    return os.getenv(name, default)


st.set_page_config(
    page_title="EV Route Planner",
    page_icon="assets/logo-qivia.png",
    layout="wide",
    initial_sidebar_state="expanded",
)

# PWA hints: when added to the iOS/Android home screen, opens fullscreen with
# the dark Qivia color in the status bar and a friendly short title.
st.markdown(
    """
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="EV Planner">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="theme-color" content="#03060D">
    <link rel="apple-touch-icon" href="assets/logo-qivia.png">
    """,
    unsafe_allow_html=True,
)

# Qivia-style dark charter: Plus Jakarta Sans + DM Serif Display, mint green accents on black.
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=DM+Serif+Display&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap');

    html, body, [class*="css"], .stApp, .stMarkdown, .stMetric, .stCaption,
    button, input, textarea, select, p, span, div, label {
        font-family: 'Plus Jakarta Sans', -apple-system, sans-serif !important;
    }
    /* On mobile, Streamlit's sidebar collapse icon doesn't render properly with our
       font overrides. Replace it with a plain "✕" via CSS pseudo-element. */
    @media (max-width: 767px) {
        [data-testid="stSidebarCollapseButton"],
        button[kind="headerNoPadding"],
        [data-testid="stSidebarCollapsedControl"] {
            position: relative !important;
            background-color: #5FFFA7 !important;
            border-radius: 8px !important;
            width: 40px !important;
            height: 40px !important;
            margin: 0.5rem !important;
        }
        [data-testid="stSidebarCollapseButton"] *,
        button[kind="headerNoPadding"] *,
        [data-testid="stSidebarCollapsedControl"] * {
            color: transparent !important;
            font-size: 0 !important;
            fill: transparent !important;
        }
        [data-testid="stSidebarCollapseButton"]::after,
        button[kind="headerNoPadding"]::after,
        [data-testid="stSidebarCollapsedControl"]::after {
            content: "✕";
            color: #03060D !important;
            font-size: 1.3rem !important;
            font-family: -apple-system, system-ui, sans-serif !important;
            font-weight: 700;
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
        }
        /* When the sidebar is collapsed, the "open" button needs a different icon */
        [data-testid="stSidebarCollapsedControl"]::after {
            content: "☰";
            font-size: 1.5rem !important;
        }
    }
    h1, h2, h3, h4 {
        font-family: 'Plus Jakarta Sans', -apple-system, sans-serif !important;
        font-weight: 700 !important;
        letter-spacing: -0.02em;
        line-height: 1.1;
        color: #FFFFFF !important;
    }
    h1 { font-size: 3rem !important; }

    /* Qivia-style highlight: mint green with subtle glow. */
    .qivia-highlight {
        color: #00E676;
        text-shadow: 0 0 12px rgba(0, 230, 118, 0.6), 0 0 28px rgba(0, 230, 118, 0.27);
    }

    /* Whole app on near-black. */
    .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
        background-color: #03060D !important;
        color: #FFFFFF;
    }

    /* Streamlit's top header: hide on desktop (the white bar), keep on mobile
       because it contains the sidebar toggle (hamburger/arrow). */
    @media (min-width: 768px) {
        header[data-testid="stHeader"] { display: none !important; }
    }
    /* On mobile, force the header to be visible with the dark theme. */
    @media (max-width: 767px) {
        header[data-testid="stHeader"] {
            display: flex !important;
            background-color: #03060D !important;
            height: 3rem !important;
        }
        header[data-testid="stHeader"] *,
        header[data-testid="stHeader"] button,
        header[data-testid="stHeader"] svg {
            color: #FFFFFF !important;
            fill: #FFFFFF !important;
        }
        /* The sidebar toggle button — make it clearly tappable. */
        [data-testid="stSidebarCollapsedControl"],
        button[kind="header"] {
            background-color: #5FFFA7 !important;
            color: #03060D !important;
            border-radius: 8px !important;
            padding: 0.4rem !important;
            margin: 0.4rem !important;
        }
        [data-testid="stSidebarCollapsedControl"] svg,
        button[kind="header"] svg {
            fill: #03060D !important;
            color: #03060D !important;
        }
    }
    div[data-testid="stToolbar"] { display: none !important; }

    /* ---------- MOBILE LAYOUT (≤ 767px) ---------- */
    @media (max-width: 767px) {
        /* Sidebar takes the full screen when opened, completely hides main behind */
        section[data-testid="stSidebar"] {
            width: 100vw !important;
            min-width: 100vw !important;
            max-width: 100vw !important;
            z-index: 999999 !important;
        }
        /* Main content uses the full viewport width when sidebar is closed */
        [data-testid="stAppViewContainer"] > section.main,
        [data-testid="stMain"] {
            padding-left: 1rem !important;
            padding-right: 1rem !important;
        }
        /* Title smaller on mobile */
        h1 { font-size: 2rem !important; }
        /* Metrics: stack 2 per row instead of 4 */
        [data-testid="stHorizontalBlock"] > [data-testid="column"] {
            min-width: 48% !important;
            flex: 1 1 48% !important;
        }
        /* Stop cards: one per row (forced full width) */
        [data-testid="stHorizontalBlock"]:has(> [data-testid="column"]:nth-child(3))
            > [data-testid="column"] {
            min-width: 100% !important;
            flex: 0 0 100% !important;
        }
        /* Tighter metric cards */
        div[data-testid="stMetric"] {
            padding: 0.5rem 0.7rem !important;
        }
        div[data-testid="stMetricValue"] {
            font-size: 1.3rem !important;
        }
        /* The comparison banner above tabs wraps */
        .stMarkdown div[style*="display:flex"] {
            flex-wrap: wrap !important;
            gap: 0.6rem !important;
        }
    }

    /* Primary buttons: mint on dark text. */
    .stButton > button[kind="primary"] {
        background-color: #5FFFA7 !important;
        color: #03060D !important;
        border: none !important;
        font-weight: 600 !important;
        border-radius: 8px !important;
        padding: 0.55rem 1.2rem !important;
        transition: background 0.15s ease;
    }
    .stButton > button[kind="primary"]:hover {
        background-color: #00E676 !important;
        color: #03060D !important;
    }
    .stButton > button { border-radius: 8px !important; }

    /* Metrics: dark card with mint values. */
    div[data-testid="stMetric"] {
        background: #0B111C;
        border: 1px solid #1A2030;
        border-radius: 10px;
        padding: 0.8rem 1rem;
    }
    div[data-testid="stMetricLabel"] { color: #9AA3B2 !important; }
    div[data-testid="stMetricValue"] {
        font-family: 'Plus Jakarta Sans', sans-serif !important;
        font-weight: 700 !important;
        color: #5FFFA7 !important;
    }

    /* Sidebar: same near-black with subtle separator. */
    section[data-testid="stSidebar"] {
        background-color: #03060D !important;
        border-right: 1px solid #1A2030;
    }
    section[data-testid="stSidebar"] *,
    section[data-testid="stSidebar"] label {
        color: #FFFFFF !important;
    }

    /* Inputs / searchbox / slider: dark surfaces. */
    input, textarea, [data-baseweb="select"] > div, [data-baseweb="input"] > div {
        background-color: #0B111C !important;
        color: #FFFFFF !important;
        border-color: #1A2030 !important;
    }

    /* streamlit-searchbox uses react-select internally — force its inner control/menu to dark gray. */
    section[data-testid="stSidebar"] [class*="-control"],
    section[data-testid="stSidebar"] [class*="-menu"],
    section[data-testid="stSidebar"] [class*="-menuList"],
    section[data-testid="stSidebar"] [class*="-option"],
    section[data-testid="stSidebar"] [class*="-singleValue"],
    section[data-testid="stSidebar"] [class*="-input"],
    section[data-testid="stSidebar"] [class*="-ValueContainer"],
    section[data-testid="stSidebar"] [class*="-IndicatorsContainer"] {
        background-color: #0B111C !important;
        color: #FFFFFF !important;
        border-color: #1A2030 !important;
    }
    section[data-testid="stSidebar"] [class*="-option"]:hover,
    section[data-testid="stSidebar"] [class*="-option--is-focused"] {
        background-color: #1A2030 !important;
    }
    section[data-testid="stSidebar"] [class*="-placeholder"] {
        color: #9AA3B2 !important;
    }

    /* Widget labels (Départ / Arrivée / Charge batterie…) in muted light gray. */
    section[data-testid="stSidebar"] [data-testid="stWidgetLabel"],
    section[data-testid="stSidebar"] label {
        color: #9AA3B2 !important;
        font-weight: 500 !important;
        font-size: 0.85rem !important;
    }
    /* But keep the section <h2> headings ("Trajet", "Véhicule"…) in white. */
    section[data-testid="stSidebar"] h1,
    section[data-testid="stSidebar"] h2,
    section[data-testid="stSidebar"] h3 {
        color: #FFFFFF !important;
    }

    /* Folium iframe: black background to blend with the page. */
    iframe { background-color: #03060D !important; }

    /* Mint accent under the page title. */
    .qivia-accent {
        height: 4px;
        width: 64px;
        background: #5FFFA7;
        border-radius: 4px;
        margin: -0.5rem 0 1.5rem 0;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

def _check_password() -> None:
    """Block the app behind a SHA-256 password gate (configured via secrets)."""
    if st.session_state.get("auth_ok"):
        return
    expected = get_secret("ACCESS_PASSWORD_HASH")
    if not expected:
        return  # No password configured — open access (useful in local dev)
    st.markdown(
        '<h1>EV <span class="qivia-highlight">Route Planner</span></h1>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="qivia-accent"></div>', unsafe_allow_html=True)
    # Stronger styling so the input is clearly visible on dark mobile screens.
    st.markdown(
        """
        <style>
        div[data-testid="stTextInput"] input {
            background-color: #0B111C !important;
            color: #FFFFFF !important;
            border: 2px solid #5FFFA7 !important;
            border-radius: 8px !important;
            padding: 0.75rem 1rem !important;
            font-size: 1.05rem !important;
        }
        div[data-testid="stTextInput"] label {
            color: #FFFFFF !important;
            font-size: 1rem !important;
            font-weight: 600 !important;
            margin-bottom: 0.5rem !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    pwd = st.text_input(
        "🔒 Code d'accès",
        type="password",
        key="access_pwd",
        placeholder="Tape ton code et appuie sur Entrée",
    )
    if pwd:
        if hashlib.sha256(pwd.encode()).hexdigest() == expected:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Code invalide")
    st.stop()


_check_password()

st.markdown(
    '<h1>EV <span class="qivia-highlight">Route Planner</span></h1>',
    unsafe_allow_html=True,
)
st.markdown('<div class="qivia-accent"></div>', unsafe_allow_html=True)
st.caption("Itinéraire et arrêts recharge optimisés")


def soc_color(soc: float) -> str:
    if soc > 50:
        return "#22c55e"
    if soc > 25:
        return "#eab308"
    if soc > 10:
        return "#f97316"
    return "#dc2626"


def render_trip(
    result: RouteResult,
    plan: TripPlan,
    origin,
    destination,
    meta: dict | None = None,
    key_suffix: str = "main",
) -> None:
    n_stops = len(plan.stops)
    total_cost = (meta or {}).get("total_cost_eur", 0.0)

    # Metrics: 4 big tiles. Breakdown moves to the caption below.
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Distance", f"{result.total_km:.0f} km")
    c2.metric("Durée totale", fmt_duration(plan.total_time_s))
    c3.metric("Coût total", f"{total_cost:.0f} €")
    c4.metric("Arrêts", str(n_stops))

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

    # Layout : carte pleine largeur, arrêts en grille horizontale en dessous.
    m = folium.Map(tiles="CartoDB dark_matter")
    pts = plan.updated_points
    all_lats: list[float] = []
    all_lngs: list[float] = []
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
                    f'font-weight:700;font-family:Plus Jakarta Sans;border:2px solid #03060D;'
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
    # Map height: shorter on mobile to leave room for stops list below.
    st_folium(m, height=400, width=None, returned_objects=[], key=f"map_{key_suffix}")

    # Arrêts en grille (4 par ligne).
    if plan.stops:
        st.markdown(
            "<div style='font-weight:700;font-size:1.05rem;margin:0.8rem 0 0.6rem 0;'>"
            "Arrêts recharge</div>",
            unsafe_allow_html=True,
        )
        extras = meta.get("stops_extras", []) if meta else []
        per_row = 4
        for row_start in range(0, len(plan.stops), per_row):
            cols = st.columns(per_row)
            for i_in_row, col in enumerate(cols):
                idx = row_start + i_in_row
                if idx >= len(plan.stops):
                    continue
                s = plan.stops[idx]
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
                        f'margin-top:0.35rem;font-size:0.85rem;">'
                        f'<span style="color:#9AA3B2;">{cost["price_per_kwh"]:.2f} €/kWh</span>'
                        f'<span style="color:#FFFFFF;font-weight:600;">'
                        f'{cost["total_eur"]:.1f} €</span></div>'
                    )
                with col:
                    st.markdown(
                        f"""
                        <div style="background:#0B111C;border:1px solid #1A2030;border-radius:10px;
                                    padding:0.85rem 1rem;height:100%;">
                          <div style="font-weight:700;color:#5FFFA7;font-size:0.95rem;
                                      margin-bottom:0.35rem;">#{idx+1} — {s.name}</div>
                          <div style="color:#9AA3B2;font-size:0.78rem;line-height:1.4;">
                            {city_line}<br>{s.operator}<br>
                            <span style="font-size:0.8rem;">{avail['label']}{avail_extra}</span>
                          </div>
                          <div style="display:flex;justify-content:space-between;margin-top:0.55rem;
                                      font-size:0.85rem;color:#FFFFFF;">
                            <span>km {s.km:.0f}</span><span>{s.power_kw:.0f} kW</span>
                          </div>
                          <div style="display:flex;justify-content:space-between;margin-top:0.35rem;
                                      font-size:0.85rem;">
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
        st.caption("Aucun arrêt nécessaire — tu arrives avec assez de batterie.")


SEARCHBOX_STYLE = {
    "searchbox": {
        "control": {
            "backgroundColor": "#03060D",
            "border": "1px solid #2A3344",
            "borderRadius": "8px",
            "minHeight": "44px",
            "boxShadow": "none",
        },
        "menu": {
            "backgroundColor": "#0B111C",
            "border": "1px solid #2A3344",
            "borderRadius": "8px",
        },
        "menuList": {"backgroundColor": "#0B111C"},
        "option": {
            "backgroundColor": "#0B111C",
            "color": "#FFFFFF",
            "padding": "10px 12px",
        },
        "singleValue": {"color": "#FFFFFF"},
        "placeholder": {"color": "#9AA3B2"},
        "input": {"color": "#FFFFFF"},
    },
}

with st.sidebar:
    st.image("assets/logo-qivia.png", width=120)
    st.markdown("<div style='height: 1.2rem'></div>", unsafe_allow_html=True)
    st.header("Trajet")
    origin = st_searchbox(
        photon_search,
        key="origin",
        placeholder="Départ",
        style_overrides=SEARCHBOX_STYLE,
    )
    destination = st_searchbox(
        photon_search,
        key="destination",
        placeholder="Arrivée",
        style_overrides=SEARCHBOX_STYLE,
    )
    soc = st.slider("Charge batterie initiale (%)", 0, 100, 80)
    st.header("Véhicule")
    st.caption(f"{TESLA_M3_LR['name']} — {TESLA_M3_LR['battery_kwh']:.0f} kWh")
    driving_style = st.radio(
        "Conduite",
        list(DRIVING_STYLES.keys()),
        index=1,
        horizontal=True,
        help="Souple = ~110 km/h, conso −15% • Normal = limites légales • "
             "Dynamique = ~140 km/h, conso +18%",
    )
    st.header("Corrections")
    use_weather = st.checkbox("Météo", value=True,
                              help="Température + vent ajustent la conso")
    use_elevation = st.checkbox("Dénivelé", value=True,
                                help="Montées coûtent, descentes récupèrent (regen)")

    corridor_km = 5.0  # km, fixed default
    go = st.button("Calculer", type="primary")

if go:
    here_key = get_secret("HERE_API_KEY")
    if not here_key:
        st.error("Aucune clé HERE_API_KEY trouvée. Voir .env.example.")
        st.stop()
    origin_coords = _parse_coords(origin)
    destination_coords = _parse_coords(destination)
    if not origin_coords or not destination_coords:
        st.error("Sélectionne un départ et une arrivée dans la sidebar.")
        st.stop()
    origin = origin_coords
    destination = destination_coords
    st.caption(f"Départ : {origin}  •  Arrivée : {destination}")

    # Adjust the consumption curve once for the whole pipeline.
    model = apply_driving_style(TESLA_M3_LR, driving_style)

    df_all = get_irve_cached()

    def pipeline(avoid_tolls: bool) -> tuple:
        """Fetch HERE → enrich → load+filter stations along this route.
        Returns (result, meta, df_filtered)."""
        result = fetch_route_here(origin, destination, soc, model, here_key, avoid_tolls=avoid_tolls)
        m: dict = {}
        if use_weather or use_elevation:
            result, m = enrich_route(
                result, model, use_weather=use_weather, use_elevation=use_elevation,
            )
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

    try:
        with st.spinner("Itinéraire avec péage…"):
            result_toll, meta_toll, df_toll = pipeline(avoid_tolls=False)
        with st.spinner("Itinéraire sans péage…"):
            result_notoll, meta_notoll, df_notoll = pipeline(avoid_tolls=True)
    except Exception as e:
        st.error(f"Routing : {e}")
        st.stop()

    # Fast plan: always on the with-toll (fastest) route.
    plan_fast = plan_trip(result_toll, df_toll, model, initial_soc_pct=soc, mode="fast")
    budget_s = plan_fast.total_time_s * 1.20

    def search_eco(result, df, time_budget_s):
        fallback = plan_trip(result, df, model, initial_soc_pct=soc, mode="eco", price_weight=40.0)
        for pw in (400.0, 250.0, 150.0, 80.0, 40.0):
            cand = plan_trip(result, df, model, initial_soc_pct=soc, mode="eco", price_weight=pw)
            if cand.feasible and cand.total_time_s <= time_budget_s:
                return cand
        return fallback

    # Eco variant A: same road as fast, eco strategy, +20% time budget.
    plan_eco_a = search_eco(result_toll, df_toll, budget_s)
    # Eco variant B: no-toll route, eco strategy, +50% time budget (no-toll roads are slower).
    plan_eco_b = search_eco(result_notoll, df_notoll, plan_fast.total_time_s * 1.50)

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

    with st.spinner("Dispo temps réel + coûts…"):
        extras_fast, recharge_fast = enrich_stops(plan_fast, df_toll)
        extras_eco_a, recharge_eco_a = enrich_stops(plan_eco_a, df_toll)
        extras_eco_b, recharge_eco_b = enrich_stops(plan_eco_b, df_notoll)

    total_fast = recharge_fast + result_toll.total_toll_eur
    total_eco_a = recharge_eco_a + result_toll.total_toll_eur
    total_eco_b = recharge_eco_b + 0.0  # no tolls on this variant

    # Eco picks the cheaper overall (recharge + toll).
    if total_eco_b < total_eco_a:
        plan_eco = plan_eco_b
        result_eco = result_notoll
        meta_eco_base = meta_notoll
        df_eco = df_notoll
        extras_eco = extras_eco_b
        recharge_eco = recharge_eco_b
        eco_uses_notoll = True
    else:
        plan_eco = plan_eco_a
        result_eco = result_toll
        meta_eco_base = meta_toll
        df_eco = df_toll
        extras_eco = extras_eco_a
        recharge_eco = recharge_eco_a
        eco_uses_notoll = False
    total_eco = recharge_eco + result_eco.total_toll_eur

    # Comparison summary line before the tabs.
    st.markdown(
        f"""
        <div style="display:flex;gap:1.5rem;margin:0.6rem 0 1rem 0;
                    color:#9AA3B2;font-size:0.9rem;">
          <span><b style="color:#5FFFA7;">Rapide</b>
            &nbsp;{fmt_duration(plan_fast.total_time_s)} &middot;
            {total_fast:.0f} € &middot;
            {len(plan_fast.stops)} arr.</span>
          <span><b style="color:#5FFFA7;">Économique</b>
            &nbsp;{fmt_duration(plan_eco.total_time_s)} &middot;
            {total_eco:.0f} € &middot;
            {len(plan_eco.stops)} arr.
            {"<span style='color:#5FFFA7;'> · sans péage</span>" if eco_uses_notoll else ""}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # On mobile, auto-collapse the sidebar so results are visible. Streamlit has no
     # native API for this; we click the toggle via JS after a short delay.
    st.markdown(
        """
        <script>
        (function() {
            if (window.innerWidth >= 768) return;
            const tryClose = () => {
                const doc = window.parent ? window.parent.document : document;
                const btn = doc.querySelector('[data-testid="stSidebarCollapseButton"]')
                          || doc.querySelector('button[kind="headerNoPadding"]');
                if (btn) { btn.click(); return true; }
                return false;
            };
            // Retry a few times — Streamlit may re-render and lose the listener.
            let attempts = 0;
            const id = setInterval(() => {
                if (tryClose() || ++attempts > 10) clearInterval(id);
            }, 150);
        })();
        </script>
        <div class="mobile-tip" style="display:none;background:#0B111C;
             border:1px solid #5FFFA7;border-radius:8px;padding:0.6rem 1rem;
             margin:0.5rem 0;color:#5FFFA7;font-size:0.9rem;">
          📱 Si tu vois encore la sidebar, touche le ✕ vert en haut à gauche
        </div>
        <style>
          @media (max-width: 767px) { .mobile-tip { display: block !important; } }
        </style>
        """,
        unsafe_allow_html=True,
    )
    tab_fast, tab_eco = st.tabs(["Rapide", "Économique"])
    with tab_fast:
        meta_fast = {
            **meta_toll,
            "stops_extras": extras_fast,
            "recharge_cost_eur": recharge_fast,
            "toll_eur": result_toll.total_toll_eur,
            "total_cost_eur": total_fast,
        }
        render_trip(result_toll, plan_fast, origin, destination, meta_fast, key_suffix="fast")
    with tab_eco:
        meta_eco = {
            **meta_eco_base,
            "stops_extras": extras_eco,
            "recharge_cost_eur": recharge_eco,
            "toll_eur": result_eco.total_toll_eur,
            "total_cost_eur": total_eco,
            "uses_notoll": eco_uses_notoll,
        }
        render_trip(result_eco, plan_eco, origin, destination, meta_eco, key_suffix="eco")
