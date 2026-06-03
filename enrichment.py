"""Post-process a RouteResult with weather (Open-Meteo) and elevation
(OpenTopoData) corrections. Both APIs are free and key-less."""
from __future__ import annotations

import math
from dataclasses import dataclass

import requests

from providers import RoutePoint, RouteResult


# Reference vehicle mass (Tesla Model 3 LR ≈ 1850 kg with driver+luggage).
DEFAULT_VEHICLE_MASS_KG = 1850.0
# Drivetrain efficiency for climbing.
CLIMB_EFFICIENCY = 0.85
# Fraction of potential energy recovered via regen on descent.
REGEN_EFFICIENCY = 0.70

# Number of weather sample points along the route.
WEATHER_SAMPLES = 6
# Max points sent to OpenTopoData in one batch (their public limit is 100).
ELEVATION_MAX_POINTS = 100


@dataclass
class WeatherSample:
    km: float
    temp_c: float
    wind_speed_kmh: float
    wind_dir_deg: float  # direction the wind is coming FROM (meteo convention)


# ---------- Math helpers ----------

def _bearing_deg(p1: tuple[float, float], p2: tuple[float, float]) -> float:
    """Initial bearing in degrees (0 = North) from p1 to p2."""
    lat1, lon1 = math.radians(p1[0]), math.radians(p1[1])
    lat2, lon2 = math.radians(p2[0]), math.radians(p2[1])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _temperature_factor(temp_c: float) -> float:
    """Multiplier on consumption due to ambient temperature.
    Reference = 20°C. Below 20: +1.5%/°C (heating, battery cold).
    Above 25: +0.8%/°C (AC)."""
    return 1.0 + max(0.0, 20.0 - temp_c) * 0.015 + max(0.0, temp_c - 25.0) * 0.008


def _wind_factor(speed_kmh: float, headwind_kmh: float) -> float:
    """Multiplier on consumption due to head/tail wind.
    Aero share grows with speed (∝ v²). At 100 km/h aero ≈ 50%."""
    if speed_kmh < 30:
        return 1.0
    aero_share = min(0.85, 0.50 * (speed_kmh / 100.0) ** 2)
    v_eff = max(1.0, speed_kmh + headwind_kmh)
    return 1.0 + aero_share * ((v_eff / speed_kmh) ** 2 - 1.0)


def _elevation_kwh(delta_m: float, mass_kg: float = DEFAULT_VEHICLE_MASS_KG) -> float:
    """Energy (kWh) to climb delta_m meters; negative on descent (regen recovery)."""
    g = 9.81
    if delta_m >= 0:
        return mass_kg * g * delta_m / 3.6e6 / CLIMB_EFFICIENCY
    else:
        return mass_kg * g * delta_m / 3.6e6 * REGEN_EFFICIENCY


def _sample_indices(n: int, max_n: int) -> list[int]:
    """Pick up to max_n evenly-spaced indices in [0, n-1]."""
    if n <= max_n:
        return list(range(n))
    return [round(i * (n - 1) / (max_n - 1)) for i in range(max_n)]


def _interp(km: float, samples: list[WeatherSample]) -> WeatherSample:
    """Linear interpolation of weather across samples (sorted by km)."""
    if km <= samples[0].km:
        return samples[0]
    if km >= samples[-1].km:
        return samples[-1]
    for i in range(len(samples) - 1):
        a, b = samples[i], samples[i + 1]
        if a.km <= km <= b.km:
            t = (km - a.km) / (b.km - a.km) if b.km != a.km else 0.0
            # Wind direction needs circular interpolation.
            da = math.radians(a.wind_dir_deg)
            db = math.radians(b.wind_dir_deg)
            x = (1 - t) * math.cos(da) + t * math.cos(db)
            y = (1 - t) * math.sin(da) + t * math.sin(db)
            wind_dir = (math.degrees(math.atan2(y, x)) + 360) % 360
            return WeatherSample(
                km=km,
                temp_c=(1 - t) * a.temp_c + t * b.temp_c,
                wind_speed_kmh=(1 - t) * a.wind_speed_kmh + t * b.wind_speed_kmh,
                wind_dir_deg=wind_dir,
            )
    return samples[-1]


# ---------- External APIs ----------

def fetch_elevations(coords: list[tuple[float, float]]) -> list[float]:
    """OpenTopoData. Batched at 100 per request."""
    elevations: list[float] = []
    for i in range(0, len(coords), ELEVATION_MAX_POINTS):
        batch = coords[i : i + ELEVATION_MAX_POINTS]
        locs = "|".join(f"{lat:.5f},{lng:.5f}" for lat, lng in batch)
        r = requests.get(
            "https://api.opentopodata.org/v1/srtm30m",
            params={"locations": locs},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        for entry in data.get("results", []):
            elev = entry.get("elevation")
            elevations.append(0.0 if elev is None else float(elev))
    return elevations


def fetch_weather(lat: float, lng: float) -> dict:
    """Open-Meteo current conditions."""
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lng,
            "current": "temperature_2m,wind_speed_10m,wind_direction_10m",
            "wind_speed_unit": "kmh",
        },
        timeout=15,
    )
    r.raise_for_status()
    cur = r.json().get("current", {})
    return {
        "temp_c": float(cur.get("temperature_2m", 20.0)),
        "wind_speed_kmh": float(cur.get("wind_speed_10m", 0.0)),
        "wind_dir_deg": float(cur.get("wind_direction_10m", 0.0)),
    }


# ---------- Orchestration ----------

def enrich_route(
    result: RouteResult,
    model: dict,
    use_weather: bool = True,
    use_elevation: bool = True,
) -> tuple[RouteResult, dict]:
    """Apply weather and/or elevation corrections segment-by-segment.
    Returns the enriched RouteResult plus a `meta` dict with summary stats
    (min/max altitude, average headwind, etc.) for display."""
    pts = result.points
    n = len(pts)
    meta: dict = {}
    if n < 2 or not (use_weather or use_elevation):
        return result, meta

    coords = [(p.lat, p.lng) for p in pts]

    # Elevations.
    elevations: list[float] = []
    if use_elevation:
        sample_idx = _sample_indices(n, ELEVATION_MAX_POINTS)
        sample_coords = [coords[i] for i in sample_idx]
        sample_elevs = fetch_elevations(sample_coords)
        # Linear interpolation of elevation back to all n points (by index along route).
        sample_kms = [pts[i].km for i in sample_idx]
        for p in pts:
            # Find surrounding samples.
            if p.km <= sample_kms[0]:
                elevations.append(sample_elevs[0])
                continue
            if p.km >= sample_kms[-1]:
                elevations.append(sample_elevs[-1])
                continue
            for j in range(len(sample_kms) - 1):
                if sample_kms[j] <= p.km <= sample_kms[j + 1]:
                    a, b = sample_kms[j], sample_kms[j + 1]
                    t = (p.km - a) / (b - a) if b != a else 0.0
                    elevations.append((1 - t) * sample_elevs[j] + t * sample_elevs[j + 1])
                    break
        meta["min_alt_m"] = min(elevations)
        meta["max_alt_m"] = max(elevations)
        meta["total_ascent_m"] = sum(
            max(0.0, elevations[i] - elevations[i - 1]) for i in range(1, n)
        )

    # Weather samples.
    weather: list[WeatherSample] = []
    if use_weather:
        sample_idx = _sample_indices(n, WEATHER_SAMPLES)
        for i in sample_idx:
            w = fetch_weather(coords[i][0], coords[i][1])
            weather.append(WeatherSample(
                km=pts[i].km,
                temp_c=w["temp_c"],
                wind_speed_kmh=w["wind_speed_kmh"],
                wind_dir_deg=w["wind_dir_deg"],
            ))
        meta["avg_temp_c"] = sum(w.temp_c for w in weather) / len(weather)
        meta["avg_wind_kmh"] = sum(w.wind_speed_kmh for w in weather) / len(weather)

    # Recompute SoC. We track both clamped soc_pct (for display) and unclamped
    # cumulative kWh consumed (for the planner).
    battery = model["battery_kwh"]
    initial_kwh = pts[0].soc_pct * battery / 100.0
    new_points: list[RoutePoint] = [
        RoutePoint(
            lat=pts[0].lat, lng=pts[0].lng, km=pts[0].km,
            soc_pct=pts[0].soc_pct, speed_kmh=pts[0].speed_kmh,
            kwh_consumed_from_start=0.0,
        )
    ]
    cumulative_kwh = 0.0  # unclamped — can grow beyond `initial_kwh`
    total_extra_kwh = 0.0

    for i in range(1, n):
        a, b = pts[i - 1], pts[i]
        # Use unclamped consumption from the raw route so deltas stay accurate
        # even when the original SoC profile flattened at 0.
        base_kwh = b.kwh_consumed_from_start - a.kwh_consumed_from_start
        speed = max(10.0, (a.speed_kmh + b.speed_kmh) / 2.0)

        mult = 1.0
        added = 0.0

        if use_weather:
            w = _interp(b.km, weather)
            mult *= _temperature_factor(w.temp_c)
            heading = _bearing_deg((a.lat, a.lng), (b.lat, b.lng))
            headwind = w.wind_speed_kmh * math.cos(math.radians(w.wind_dir_deg - heading))
            mult *= _wind_factor(speed, headwind)

        if use_elevation and elevations:
            added = _elevation_kwh(elevations[i] - elevations[i - 1])

        adj_kwh = base_kwh * mult + added
        total_extra_kwh += (adj_kwh - base_kwh)
        cumulative_kwh += adj_kwh
        soc_pct = max(0.0, (initial_kwh - cumulative_kwh) / battery * 100.0)
        new_points.append(RoutePoint(
            lat=b.lat, lng=b.lng, km=b.km, soc_pct=soc_pct, speed_kmh=b.speed_kmh,
            kwh_consumed_from_start=cumulative_kwh,
        ))

    first_below = next((p for p in new_points if p.soc_pct < 10.0), None)
    base_consumption = result.total_consumption_kwh
    enriched = RouteResult(
        provider=result.provider,
        points=new_points,
        total_km=result.total_km,
        total_consumption_kwh=base_consumption + total_extra_kwh,
        soc_at_arrival_pct=new_points[-1].soc_pct,
        first_below_10pct=first_below,
        total_duration_s=result.total_duration_s,
        total_toll_eur=result.total_toll_eur,
        avoids_tolls=result.avoids_tolls,
    )
    meta["extra_kwh_vs_base"] = total_extra_kwh
    return enriched, meta
