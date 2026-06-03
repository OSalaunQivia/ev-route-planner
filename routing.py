"""Charging stops planner.

Greedy algorithm: walk along the SoC profile produced by the routing+enrichment
pipeline. Whenever SoC would drop below `min_soc_pct`, look back over a window
to pick the best charging station (highest scoring combination of power and
position), insert a stop, and shift the rest of the profile upward by the
charged amount. Repeat until destination is reached with a comfortable margin.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from providers import RoutePoint, RouteResult


Mode = Literal["fast", "eco"]

# Tuning parameters per planning mode.
#   target_leave_soc_pct      — what SoC we leave each charger at
#   min_arrival_destination   — required SoC at the end of the trip
#   power_weight              — boosts higher-kW stations in candidate score
#   position_weight           — boosts later stations in the lookback window
#   price_weight              — penalises expensive stations (only used in eco)
MODE_PARAMS: dict[str, dict[str, float]] = {
    "fast": {
        "target_leave_soc_pct": 70.0,
        "min_arrival_destination_pct": 10.0,
        "power_weight": 0.6,
        "position_weight": 100.0,
        "price_weight": 0.0,
    },
    "eco": {
        "target_leave_soc_pct": 85.0,
        "min_arrival_destination_pct": 15.0,
        "power_weight": 0.3,
        "position_weight": 80.0,
        "price_weight": 200.0,
    },
}


def _effective_kw(station_power: float, vehicle_max_dc: float, target_leave_pct: float) -> float:
    """Realistic average DC charging power 10→target.
    - Slow stations relative to vehicle peak ≈ constant power, factor close to 1.
    - Fast stations relative to peak ≈ taper hard above 60%, factor lower.
    - Higher target (more time in the slow tail) lowers the factor further.
    """
    limited = min(station_power, vehicle_max_dc)
    ratio = limited / max(vehicle_max_dc, 1.0)
    factor = 0.95 - 0.55 * ratio
    target_penalty = max(0.0, (target_leave_pct - 80.0) * 0.01)
    factor = max(0.30, factor - target_penalty)
    return limited * factor


@dataclass
class ChargingStop:
    km: float
    lat: float
    lng: float
    name: str
    operator: str
    city: str
    power_kw: float
    soc_arrival_pct: float
    soc_leave_pct: float
    kwh_added: float
    charge_time_min: float


@dataclass
class TripPlan:
    stops: list[ChargingStop]
    feasible: bool
    reason: str
    arrival_soc_pct: float
    drive_time_s: float
    charge_time_s: float
    total_time_s: float
    updated_points: list[RoutePoint]  # SoC profile after charging shifts
    mode: str = "fast"


def plan_trip(
    result: RouteResult,
    stations_df: pd.DataFrame,
    model: dict,
    initial_soc_pct: float,
    mode: Mode = "fast",
    min_soc_pct: float = 10.0,
    lookback_km: float = 200.0,
    max_stops: int = 12,
    **overrides: float,
) -> TripPlan:
    params = {**MODE_PARAMS[mode], **overrides}
    target_leave_soc_pct = params["target_leave_soc_pct"]
    min_arrival_destination_pct = params["min_arrival_destination_pct"]
    battery_kwh = model["battery_kwh"]
    vehicle_max_dc = model.get("max_dc_kw", 150.0)
    initial_kwh = initial_soc_pct / 100.0 * battery_kwh

    pts = result.points
    n = len(pts)
    # Cumulative kWh added by all charging stops applied so far, per route index.
    # (Indexed by point — gets incremented from stop_idx onward when we add a stop.)
    charge_added = [0.0] * n

    def soc_at(idx: int) -> float:
        """SoC % at point idx, accounting for already-planned stops.
        Can be negative — that's the "virtual" deficit, used by the planner."""
        net_kwh = initial_kwh + charge_added[idx] - pts[idx].kwh_consumed_from_start
        return net_kwh / battery_kwh * 100.0

    def first_below(threshold_pct: float) -> int | None:
        for i in range(n):
            if soc_at(i) < threshold_pct:
                return i
        return None

    def idx_at_km(target_km: float) -> int:
        # Linear scan — n is at most a few thousand.
        for i in range(n - 1):
            if pts[i].km <= target_km < pts[i + 1].km:
                return i
        return n - 1

    stops: list[ChargingStop] = []
    feasible = True
    reason = ""
    have_stations = not stations_df.empty and "km_along_route" in stations_df.columns

    for _ in range(max_stops + 1):
        if soc_at(n - 1) >= min_arrival_destination_pct:
            break

        critical_idx = first_below(min_soc_pct)
        critical_km = pts[critical_idx].km if critical_idx is not None else pts[-1].km

        if not have_stations:
            feasible = False
            reason = "Aucune borne dans le corridor — élargis le corridor ou les catégories."
            break

        window_start = max(0.0, critical_km - lookback_km)
        cands = stations_df[
            (stations_df["km_along_route"] >= window_start)
            & (stations_df["km_along_route"] < critical_km)
        ]
        # Exclude stations that are at or before the last stop's km (no going backwards).
        if stops:
            cands = cands[cands["km_along_route"] > stops[-1].km + 5.0]
        if cands.empty:
            feasible = False
            reason = (
                f"Pas de borne dans les {lookback_km:.0f} km avant le km "
                f"{critical_km:.0f} — élargis le corridor ou les catégories."
            )
            break

        # Score = mix of power, position lateness, and (eco only) price penalty.
        cands = cands.copy()
        prices = (
            cands["price_per_kwh"].fillna(0.55)
            if "price_per_kwh" in cands.columns
            else 0.55
        )
        cands["score"] = (
            cands["puissance_nominale"].fillna(0).clip(upper=vehicle_max_dc) * params["power_weight"]
            + (cands["km_along_route"] / max(critical_km, 1.0)) * params["position_weight"]
            - prices * params["price_weight"]
        )
        best = cands.sort_values("score", ascending=False).iloc[0]

        stop_km = float(best["km_along_route"])
        stop_idx = idx_at_km(stop_km)
        soc_arrival = soc_at(stop_idx)
        # If the station is too early (we'd arrive nearly full), it's a poor choice —
        # but the algorithm should still terminate. Charge anyway up to target.
        soc_gain = max(0.0, target_leave_soc_pct - soc_arrival)
        if soc_gain <= 0.5:
            feasible = False
            reason = "Station trop précoce — planification incohérente."
            break

        kwh_added = soc_gain / 100.0 * battery_kwh
        station_power = float(best.get("puissance_nominale") or 50.0)
        effective_kw = _effective_kw(station_power, vehicle_max_dc, target_leave_soc_pct)
        charge_time_min = kwh_added / effective_kw * 60.0

        stops.append(ChargingStop(
            km=stop_km,
            lat=float(best["lat"]),
            lng=float(best["lng"]),
            name=str(best.get("nom_station") or best.get("nom_operateur") or "Borne"),
            operator=str(best.get("nom_operateur") or "—"),
            city=str(best.get("consolidated_commune") or ""),
            power_kw=station_power,
            soc_arrival_pct=max(0.0, soc_arrival),
            soc_leave_pct=target_leave_soc_pct,
            kwh_added=kwh_added,
            charge_time_min=charge_time_min,
        ))
        # Apply the added kWh to all points from stop_idx onward.
        for i in range(stop_idx, n):
            charge_added[i] += kwh_added
    else:
        feasible = False
        reason = f"Plus de {max_stops} arrêts nécessaires — trajet probablement irréaliste."

    updated_points = [
        RoutePoint(
            lat=p.lat, lng=p.lng, km=p.km,
            soc_pct=max(0.0, soc_at(i)),
            speed_kmh=p.speed_kmh,
            kwh_consumed_from_start=p.kwh_consumed_from_start,
        )
        for i, p in enumerate(pts)
    ]

    drive_time_s = result.total_duration_s
    charge_time_s = sum(s.charge_time_min for s in stops) * 60.0
    return TripPlan(
        stops=stops,
        feasible=feasible,
        reason=reason,
        arrival_soc_pct=max(0.0, soc_at(n - 1)),
        drive_time_s=drive_time_s,
        charge_time_s=charge_time_s,
        total_time_s=drive_time_s + charge_time_s,
        updated_points=updated_points,
        mode=mode,
    )


def fmt_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    if h:
        return f"{h} h {m:02d}"
    return f"{m} min"
