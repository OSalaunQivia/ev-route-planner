"""Real-time charging station availability via TomTom Search API.

The IRVE dataset is static (locations + power), so we cross-query TomTom EV
Search to get the per-connector availability. Matching is done by proximity
(<= 250 m) and an optional name hint.
"""
from __future__ import annotations

import requests
import streamlit as st


STATUS_LABELS = {
    "available": "🟢 Disponible",
    "occupied": "🟠 Occupée",
    "out_of_service": "🔴 HS",
    "unknown": "⚪ Inconnue",
}


@st.cache_data(ttl=120, show_spinner=False)
def fetch_availability(
    lat: float,
    lng: float,
    name_hint: str,
    api_key: str,
) -> dict:
    """Return {status, label, n_total, n_available, n_occupied} for a station."""
    fallback = {"status": "unknown", "label": STATUS_LABELS["unknown"]}
    if not api_key:
        return fallback
    try:
        r = requests.get(
            "https://api.tomtom.com/search/2/categorySearch/electric%20vehicle%20station.json",
            params={"lat": lat, "lon": lng, "radius": 300, "limit": 5, "key": api_key},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return fallback

        # Pick closest; refine by name keyword if a hint is provided.
        best = results[0]
        if name_hint:
            words = [w for w in name_hint.lower().split() if len(w) > 2]
            for res in results:
                poi_name = (res.get("poi", {}).get("name") or "").lower()
                if any(w in poi_name for w in words):
                    best = res
                    break

        avail_id = (
            best.get("dataSources", {}).get("chargingAvailability", {}).get("id")
        )
        if not avail_id:
            return fallback

        r2 = requests.get(
            "https://api.tomtom.com/search/2/chargingAvailability.json",
            params={"chargingAvailability": avail_id, "key": api_key},
            timeout=10,
        )
        r2.raise_for_status()
        data = r2.json()

        n_avail = n_occ = n_oos = n_unk = n_total = 0
        for connector in data.get("connectors", []):
            cur = connector.get("availability", {}).get("current", {})
            n_avail += cur.get("available", 0)
            n_occ += cur.get("occupied", 0)
            n_oos += cur.get("outOfService", 0)
            n_unk += cur.get("unknown", 0)
            n_total += connector.get("total", 0)

        if n_total == 0:
            return fallback
        if n_avail > 0:
            status = "available"
        elif n_occ > 0:
            status = "occupied"
        elif n_oos > 0:
            status = "out_of_service"
        else:
            status = "unknown"

        return {
            "status": status,
            "label": STATUS_LABELS[status],
            "n_total": n_total,
            "n_available": n_avail,
            "n_occupied": n_occ,
        }
    except Exception as e:
        print(f"[availability] {e}")
        return fallback
