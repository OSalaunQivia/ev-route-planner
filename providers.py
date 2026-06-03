"""HERE + TomTom EV routing wrappers with a common interface."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import flexpolyline as fp
import requests


# Tesla Model 3 Long Range reference profile.
# Speeds in km/h, consumption in kWh/km (flat road, mild climate, no aux apart from `aux_kw`).
# Driving style multipliers applied to the consumption curve.
# Sources: EVDB / Fastned real-world consumption tests at 110/130/140 km/h.
DRIVING_STYLES: dict[str, float] = {
    "Souple": 0.85,
    "Normal": 1.0,
    "Dynamique": 1.18,
}


def apply_driving_style(model: dict, style: str) -> dict:
    """Return a copy of the model with its consumption_curve scaled by the
    driving-style factor. Unknown styles fall back to 1.0."""
    factor = DRIVING_STYLES.get(style, 1.0)
    if factor == 1.0:
        return model
    return {
        **model,
        "consumption_curve": [
            (speed, kwh_per_km * factor)
            for speed, kwh_per_km in model["consumption_curve"]
        ],
    }


TESLA_M3_LR = {
    "name": "Tesla Model 3 LR",
    "battery_kwh": 75.0,
    "aux_kw": 1.0,
    "max_dc_kw": 250.0,  # peak DC charging accepted by the car
    "consumption_curve": [
        (0, 0.130),
        (30, 0.130),
        (50, 0.140),
        (80, 0.150),
        (100, 0.165),
        (120, 0.190),
        (130, 0.210),
    ],
}


@dataclass
class RoutePoint:
    lat: float
    lng: float
    km: float
    soc_pct: float
    speed_kmh: float = 90.0  # average speed of the section this point belongs to
    # Cumulative kWh consumed since the start of the route, BEFORE clamping SoC at 0.
    # Used by the planner to compute "virtual" deficits beyond the car's actual range.
    kwh_consumed_from_start: float = 0.0


@dataclass
class RouteResult:
    provider: str
    points: list[RoutePoint]
    total_km: float
    total_consumption_kwh: float
    soc_at_arrival_pct: float
    first_below_10pct: Optional[RoutePoint]
    total_duration_s: float = 0.0
    total_toll_eur: float = 0.0
    avoids_tolls: bool = False


def _haversine_km(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    r = 6371.0
    lat1, lng1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lng2 = math.radians(p2[0]), math.radians(p2[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _here_freeflow_table(curve: list[tuple[float, float]]) -> str:
    return ",".join(f"{int(s)},{c:.4f}" for s, c in curve)


def _tomtom_consumption_pairs(curve: list[tuple[float, float]]) -> str:
    # TomTom wants kWh per 100 km, speed >= 10 km/h.
    pairs = [(s, c * 100) for s, c in curve if s >= 10]
    return ":".join(f"{int(s)},{c:.2f}" for s, c in pairs)


def _distribute_points(
    coords: list[tuple[float, float]],
    section_km: float,
    section_kwh: float,
    section_speed_kmh: float,
    cumulative_km: float,
    cumulative_kwh: float,
    initial_charge_kwh: float,
    battery_kwh: float,
) -> list[RoutePoint]:
    """Assign a (km, SoC, speed) to each polyline point by interpolating the
    section's distance and consumption proportionally to the haversine distance
    between consecutive points."""
    n = len(coords)
    if n < 2:
        return []
    seg = [_haversine_km(coords[i], coords[i + 1]) for i in range(n - 1)]
    total_seg = sum(seg) or 1.0
    out: list[RoutePoint] = []
    running = 0.0
    for i, (lat, lng) in enumerate(coords):
        if i > 0:
            running += seg[i - 1]
        frac = running / total_seg
        km_at = cumulative_km + section_km * frac
        kwh_at = cumulative_kwh + section_kwh * frac
        soc_kwh = initial_charge_kwh - kwh_at
        soc_pct = max(0.0, soc_kwh / battery_kwh * 100.0)
        out.append(RoutePoint(
            lat=lat, lng=lng, km=km_at, soc_pct=soc_pct,
            speed_kmh=section_speed_kmh,
            kwh_consumed_from_start=kwh_at,
        ))
    return out


def fetch_route_here(
    origin: tuple[float, float],
    destination: tuple[float, float],
    initial_soc_pct: float,
    model: dict,
    api_key: str,
    avoid_tolls: bool = False,
) -> RouteResult:
    initial_charge = model["battery_kwh"] * initial_soc_pct / 100.0
    params = {
        "transportMode": "car",
        "origin": f"{origin[0]},{origin[1]}",
        "destination": f"{destination[0]},{destination[1]}",
        "ev[freeFlowSpeedTable]": _here_freeflow_table(model["consumption_curve"]),
        # Same consumption curve reused for traffic-slowed segments (approximation).
        "ev[trafficSpeedTable]": _here_freeflow_table(model["consumption_curve"]),
        "ev[initialCharge]": initial_charge,
        "ev[maxCharge]": model["battery_kwh"],
        "ev[auxiliaryConsumption]": model["aux_kw"],
        # Real-time traffic: HERE uses traffic data when departureTime is set.
        "departureTime": datetime.now().astimezone().isoformat(timespec="seconds"),
        "return": "polyline,summary,travelSummary,tolls",
        "tolls[currency]": "EUR",
        "apiKey": api_key,
    }
    if avoid_tolls:
        params["avoid[features]"] = "tollRoad"
    r = requests.get("https://router.hereapi.com/v8/routes", params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("routes"):
        raise RuntimeError(f"HERE returned no routes: {data}")
    route = data["routes"][0]

    points: list[RoutePoint] = []
    cum_km = 0.0
    cum_kwh = 0.0
    cum_dur_s = 0.0
    total_toll_eur = 0.0
    for section in route["sections"]:
        decoded = fp.decode(section["polyline"])
        coords = [(p[0], p[1]) for p in decoded]
        ts = section.get("travelSummary", {})
        section_km = ts.get("length", 0) / 1000.0
        section_kwh = ts.get("consumption", 0.0)
        section_dur_s = ts.get("duration", 0) or 1
        section_speed_kmh = section_km / (section_dur_s / 3600.0)
        pts = _distribute_points(
            coords, section_km, section_kwh, section_speed_kmh,
            cum_km, cum_kwh, initial_charge, model["battery_kwh"],
        )
        # Avoid duplicating the joining point between sections.
        if points and pts:
            pts = pts[1:]
        points.extend(pts)
        cum_km += section_km
        cum_kwh += section_kwh
        cum_dur_s += section_dur_s
        # Sum toll fares for this section (EUR).
        for toll in section.get("tolls", []) or []:
            for fare in toll.get("fares", []) or []:
                price = fare.get("price") or {}
                if price.get("currency") == "EUR":
                    try:
                        total_toll_eur += float(price.get("value") or 0)
                    except (TypeError, ValueError):
                        pass

    first_below = next((p for p in points if p.soc_pct < 10.0), None)
    return RouteResult(
        provider="HERE",
        points=points,
        total_km=cum_km,
        total_consumption_kwh=cum_kwh,
        soc_at_arrival_pct=points[-1].soc_pct if points else 0.0,
        first_below_10pct=first_below,
        total_duration_s=cum_dur_s,
        total_toll_eur=total_toll_eur,
        avoids_tolls=avoid_tolls,
    )


def fetch_route_tomtom(
    origin: tuple[float, float],
    destination: tuple[float, float],
    initial_soc_pct: float,
    model: dict,
    api_key: str,
) -> RouteResult:
    initial_charge = model["battery_kwh"] * initial_soc_pct / 100.0
    url = (
        f"https://api.tomtom.com/routing/1/calculateRoute/"
        f"{origin[0]},{origin[1]}:{destination[0]},{destination[1]}/json"
    )
    params = {
        "key": api_key,
        "vehicleEngineType": "electric",
        "constantSpeedConsumptionInkWhPerHundredkm":
            _tomtom_consumption_pairs(model["consumption_curve"]),
        "currentChargeInkWh": initial_charge,
        "maxChargeInkWh": model["battery_kwh"],
        "auxiliaryPowerInkW": model["aux_kw"],
        "routeRepresentation": "polyline",
        # Real-time traffic on (affects route choice + duration + consumption).
        "traffic": "true",
        "departAt": "now",
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("routes"):
        raise RuntimeError(f"TomTom returned no routes: {data}")
    route = data["routes"][0]

    # TomTom puts EV consumption at the ROUTE level (route.summary) under the field
    # `batteryConsumptionInkWh`. Fallbacks: consumptionInkWh, then derive from remaining charge.
    route_summary = route.get("summary", {})
    route_total_kwh = (
        route_summary.get("batteryConsumptionInkWh")
        or route_summary.get("consumptionInkWh")
    )
    if route_total_kwh is None:
        remaining = route_summary.get("remainingChargeAtArrivalInkWh")
        if remaining is not None:
            route_total_kwh = initial_charge - remaining
    if route_total_kwh is None:
        route_total_kwh = 0.0

    # Distribute the route-level consumption across legs proportionally to leg distance.
    leg_kms = [leg.get("summary", {}).get("lengthInMeters", 0) / 1000.0 for leg in route["legs"]]
    total_leg_km = sum(leg_kms) or 1.0

    points: list[RoutePoint] = []
    cum_km = 0.0
    cum_kwh = 0.0
    for leg, leg_km in zip(route["legs"], leg_kms):
        summary = leg.get("summary", {})
        leg_kwh = (
            summary.get("batteryConsumptionInkWh")
            or summary.get("consumptionInkWh")
        )
        if leg_kwh is None:
            leg_kwh = route_total_kwh * (leg_km / total_leg_km)
        leg_dur_s = summary.get("travelTimeInSeconds", 0) or 1
        leg_speed_kmh = leg_km / (leg_dur_s / 3600.0)
        coords = [(p["latitude"], p["longitude"]) for p in leg.get("points", [])]
        pts = _distribute_points(
            coords, leg_km, leg_kwh, leg_speed_kmh,
            cum_km, cum_kwh, initial_charge, model["battery_kwh"],
        )
        if points and pts:
            pts = pts[1:]
        points.extend(pts)
        cum_km += leg_km
        cum_kwh += leg_kwh

    first_below = next((p for p in points if p.soc_pct < 10.0), None)
    return RouteResult(
        provider="TomTom",
        points=points,
        total_km=cum_km,
        total_consumption_kwh=cum_kwh,
        soc_at_arrival_pct=points[-1].soc_pct if points else 0.0,
        first_below_10pct=first_below,
    )


def geocode(query: str) -> tuple[float, float]:
    """Accepts 'lat,lng' or a free-text address (Nominatim/OSM)."""
    if "," in query:
        try:
            a, b = query.split(",", 1)
            return float(a.strip()), float(b.strip())
        except ValueError:
            pass
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": query, "format": "json", "limit": 1},
        headers={"User-Agent": "ev-route-planner/0.1"},
        timeout=15,
    )
    r.raise_for_status()
    results = r.json()
    if not results:
        raise RuntimeError(f"No geocoding result for {query!r}")
    return float(results[0]["lat"]), float(results[0]["lon"])
