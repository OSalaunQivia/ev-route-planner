"""Load and query the IRVE consolidated dataset (data.gouv.fr).

The official French register of public EV charging stations. ~140k points,
updated daily. Free, no key, no rate limit.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import requests

from providers import RoutePoint

CACHE_PATH = Path(__file__).parent / "data" / "irve.csv"
DATASET_API = (
    "https://www.data.gouv.fr/api/1/datasets/"
    "fichier-consolide-des-bornes-de-recharge-pour-vehicules-electriques/"
)


# ---------- Download ----------

def _resolve_irve_url() -> str:
    """Find the URL of the latest IRVE static-data CSV from the dataset metadata.

    The dataset contains several CSV resources: the actual IRVE data file plus
    validation/quality reports. We discriminate by:
      1. The resource's declared schema (`etalab/schema-irve-statique`), best signal.
      2. URL/title contains `irve-statique` and NOT `rapport`/`report`/`validation`/`status`.
    """
    r = requests.get(DATASET_API, timeout=20)
    r.raise_for_status()
    data = r.json()
    csv_resources = [
        res for res in data.get("resources", [])
        if res.get("format", "").lower() == "csv"
    ]

    def has_irve_schema(res: dict) -> bool:
        schema = res.get("schema") or {}
        if isinstance(schema, dict):
            return "irve-statique" in (schema.get("name") or "").lower()
        return False

    BLACKLIST = ("rapport", "report", "validation", "status", "schema-status")

    def looks_like_data(res: dict) -> bool:
        title = (res.get("title") or "").lower()
        url = (res.get("url") or "").lower()
        if any(b in title for b in BLACKLIST):
            return False
        return "irve-statique" in title or "irve-statique" in url

    candidates = [r for r in csv_resources if has_irve_schema(r)]
    if not candidates:
        candidates = [r for r in csv_resources if looks_like_data(r)]
    if not candidates:
        raise RuntimeError(
            "Pas de ressource CSV IRVE identifiable. Titres trouvés : "
            + ", ".join(repr(r.get("title")) for r in csv_resources[:10])
        )
    candidates.sort(key=lambda r: r.get("last_modified", ""), reverse=True)
    return candidates[0]["url"]


def _download_irve(target: Path) -> None:
    url = _resolve_irve_url()
    print(f"[irve] downloading {url}…")
    r = requests.get(url, timeout=180)
    r.raise_for_status()
    target.write_bytes(r.content)


def load_irve(force_refresh: bool = False) -> pd.DataFrame:
    """Download (once) and load the IRVE consolidated CSV.
    If the cached file turns out to be the wrong CSV (no lat/lng columns), it is
    silently dropped and re-downloaded."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if force_refresh or not CACHE_PATH.exists():
        _download_irve(CACHE_PATH)

    df = pd.read_csv(CACHE_PATH, low_memory=False)
    lat_col = _first_present(df, ["consolidated_latitude", "ylatitude", "latitude"])
    lng_col = _first_present(df, ["consolidated_longitude", "xlongitude", "longitude"])
    if lat_col is None or lng_col is None:
        # Wrong file in cache — drop and try once more with fresh download.
        print(f"[irve] cache mismatch, re-downloading. Bad columns: {list(df.columns)[:6]}")
        CACHE_PATH.unlink(missing_ok=True)
        _download_irve(CACHE_PATH)
        df = pd.read_csv(CACHE_PATH, low_memory=False)
        lat_col = _first_present(df, ["consolidated_latitude", "ylatitude", "latitude"])
        lng_col = _first_present(df, ["consolidated_longitude", "xlongitude", "longitude"])
        if lat_col is None or lng_col is None:
            raise RuntimeError(
                f"IRVE : colonnes lat/lng introuvables même après re-download. "
                f"Colonnes : {list(df.columns)[:10]}"
            )
    power_col = _first_present(df, ["puissance_nominale", "puissance_max"])
    df = df.rename(columns={lat_col: "lat", lng_col: "lng"})
    if power_col and power_col != "puissance_nominale":
        df = df.rename(columns={power_col: "puissance_nominale"})
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lng"] = pd.to_numeric(df["lng"], errors="coerce")
    df["puissance_nominale"] = pd.to_numeric(df.get("puissance_nominale"), errors="coerce")
    df = df.dropna(subset=["lat", "lng"])
    return df


def _first_present(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


# ---------- Filtering ----------

def filter_corridor(
    df: pd.DataFrame,
    route_points: list[RoutePoint],
    corridor_km: float = 5.0,
) -> pd.DataFrame:
    """Keep only stations within `corridor_km` of the route polyline."""
    if df.empty or len(route_points) < 2:
        return df.iloc[:0]

    lats = np.array([p.lat for p in route_points])
    lngs = np.array([p.lng for p in route_points])

    # Coarse bbox first.
    pad = corridor_km / 111.0  # ~1° latitude ≈ 111 km
    bbox = df[
        df["lat"].between(lats.min() - pad, lats.max() + pad)
        & df["lng"].between(lngs.min() - pad, lngs.max() + pad)
    ].copy()
    if bbox.empty:
        return bbox

    # Downsample the route to keep the haversine loop fast.
    n = len(route_points)
    step = max(1, n // 200)
    s_lats = np.radians(lats[::step])
    s_lngs = np.radians(lngs[::step])
    s_kms = np.array([p.km for p in route_points])[::step]
    s_socs = np.array([p.soc_pct for p in route_points])[::step]

    station_lats = np.radians(bbox["lat"].to_numpy())
    station_lngs = np.radians(bbox["lng"].to_numpy())

    min_dists = np.empty(len(bbox))
    nearest_km = np.empty(len(bbox))
    nearest_soc = np.empty(len(bbox))
    for i in range(len(bbox)):
        dlat = s_lats - station_lats[i]
        dlng = s_lngs - station_lngs[i]
        a = np.sin(dlat / 2) ** 2 + np.cos(station_lats[i]) * np.cos(s_lats) * np.sin(dlng / 2) ** 2
        d = 2 * 6371.0 * np.arcsin(np.sqrt(a))
        idx = int(d.argmin())
        min_dists[i] = d[idx]
        nearest_km[i] = s_kms[idx]
        nearest_soc[i] = s_socs[idx]

    bbox["distance_to_route_km"] = min_dists
    bbox["km_along_route"] = nearest_km
    bbox["soc_when_passing_pct"] = nearest_soc
    return bbox[bbox["distance_to_route_km"] <= corridor_km].copy()


def _has_connector_col(df: pd.DataFrame, key: str) -> str | None:
    """Find IRVE connector flag column by fragment (type_2, ccs, chademo, ef)."""
    for col in df.columns:
        if col.startswith("prise_type_") and key in col.lower():
            return col
    return None


CONNECTOR_LABELS = {
    "Type 2": "type_2",
    "Combo CCS": "ccs",
    "CHAdeMO": "chademo",
    "Type E/F (domestique)": "ef",
}


# Catégories de puissance — référentiel Avere-France / IRVE.
# (label, min_kw_inclusive, max_kw_exclusive, color)
POWER_CATEGORIES: list[tuple[str, float, float, str]] = [
    ("Normale",        0.0,    7.4,    "#6B7280"),  # gris
    ("Accélérée",      7.4,   22.0,    "#60A5FA"),  # bleu
    ("Rapide",        22.0,   50.0,    "#FBBF24"),  # jaune
    ("HPC",           50.0,  150.0,    "#00E676"),  # vert Qivia secondaire
    ("Ultra-rapide", 150.0,  10_000,   "#5FFFA7"),  # mint Qivia primaire
]
POWER_CATEGORY_LABELS = [c[0] for c in POWER_CATEGORIES]


def categorize_power(kw: float) -> str | None:
    """Return the category label of a given power in kW, or None if NaN."""
    if pd.isna(kw):
        return None
    for label, lo, hi, _ in POWER_CATEGORIES:
        if lo <= kw < hi:
            return label
    return None


def apply_filters(
    df: pd.DataFrame,
    categories: list[str] | None = None,
    connectors: list[str] | None = None,
    operators: list[str] | None = None,
    only_24_7: bool = False,
) -> pd.DataFrame:
    out = df
    if categories and "puissance_nominale" in out.columns:
        # Keep rows whose power falls in any of the selected categories.
        mask = pd.Series(False, index=out.index)
        for label in categories:
            cat = next((c for c in POWER_CATEGORIES if c[0] == label), None)
            if cat is None:
                continue
            _, lo, hi, _ = cat
            mask = mask | out["puissance_nominale"].between(lo, hi, inclusive="left")
        out = out[mask]
    if connectors:
        mask = pd.Series(False, index=out.index)
        for label in connectors:
            key = CONNECTOR_LABELS.get(label)
            if key is None:
                continue
            col = _has_connector_col(out, key)
            if col is None:
                continue
            vals = out[col].astype(str).str.lower()
            mask = mask | vals.isin(["1", "true", "vrai", "oui"])
        out = out[mask]
    if operators and "nom_operateur" in out.columns:
        out = out[out["nom_operateur"].isin(operators)]
    if only_24_7 and "horaires" in out.columns:
        h = out["horaires"].astype(str).str.lower()
        out = out[h.str.contains("24/7|24h/24|mo-su 00:00-24:00", regex=True, na=False)]
    return out


def top_operators(df: pd.DataFrame, n: int = 20) -> list[str]:
    if "nom_operateur" not in df.columns:
        return []
    return df["nom_operateur"].value_counts().head(n).index.tolist()


def available_connectors(df: pd.DataFrame) -> list[str]:
    """Return the labels of connector types actually present in the dataset."""
    labels = []
    for label, key in CONNECTOR_LABELS.items():
        if _has_connector_col(df, key):
            labels.append(label)
    return labels


def power_color(power_kw: float) -> str:
    """Color a station marker by its power category (Avere-France referential)."""
    label = categorize_power(power_kw)
    if label is None:
        return "#666"
    for cat_label, _, _, color in POWER_CATEGORIES:
        if cat_label == label:
            return color
    return "#666"
