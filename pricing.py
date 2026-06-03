"""Per-kWh price estimation for charging stops.

No reliable open-data source for live tariffs in France. Strategy:
  1. Look up the operator in a maintained dictionary of public ad-hoc prices.
  2. Fall back to a power-tier default.
  3. Optionally parse IRVE's free-text `tarification` field for explicit €/kWh.

Prices below are public ad-hoc tariffs as of mid-2025; refresh periodically.
"""
from __future__ import annotations

import re

# Operator → €/kWh ad-hoc (carte bleue, sans abonnement).
OPERATOR_PRICES = {
    "Ionity": 0.59,
    "Tesla": 0.40,            # Supercharger pour non-Tesla
    "TotalEnergies": 0.59,
    "Total Energies": 0.59,
    "Allego": 0.59,
    "Engie": 0.49,
    "Engie Vianeo": 0.49,
    "Izivia": 0.55,
    "Fastned": 0.65,
    "PowerDot": 0.49,
    "Power Dot": 0.49,
    "ChargePoint": 0.55,
    "Lidl": 0.39,
    "Lidl France": 0.39,
    "E.Leclerc": 0.40,
    "Leclerc": 0.40,
    "Carrefour": 0.45,
    "Auchan": 0.45,
    "Avia": 0.45,
    "Bornes Avia": 0.45,
    "Driveco": 0.49,
    "Electra": 0.49,
    "EVgo": 0.59,
    "Shell Recharge": 0.59,
    "BP Pulse": 0.59,
    "Freshmile": 0.45,
    "Sodetrel": 0.49,
}

# Fallback by power tier (when operator unknown).
DEFAULT_BY_TIER = [
    (22.0,  0.30),   # accélérée
    (50.0,  0.45),   # rapide
    (150.0, 0.55),   # HPC
    (350.0, 0.65),   # ultra-rapide
    (1e9,   0.69),
]


def estimate_price_per_kwh(operator, power_kw: float) -> tuple[float, str]:
    """Return (€/kWh, source) where source ∈ {'operator', 'tier'}.
    Accepts any type for `operator` (NaN, None, numbers, strings)."""
    if isinstance(operator, str) and operator.strip():
        op = operator.strip()
        if op in OPERATOR_PRICES:
            return OPERATOR_PRICES[op], "operator"
        op_low = op.lower()
        for known, price in OPERATOR_PRICES.items():
            if known.lower() in op_low or op_low in known.lower():
                return price, "operator"
    try:
        p = float(power_kw)
    except (TypeError, ValueError):
        p = 50.0
    for thresh, price in DEFAULT_BY_TIER:
        if p <= thresh:
            return price, "tier"
    return 0.69, "tier"


_PRICE_RE = re.compile(r"(\d+[\.,]?\d*)\s*(?:€|EUR|euro)\s*/?\s*kWh", re.IGNORECASE)


def parse_irve_tarification(text) -> float | None:
    """Best-effort extraction of an explicit €/kWh value from the IRVE
    `tarification` free-text field. Returns None when not parseable.
    Accepts any input (NaN, None, numbers, strings) — only strings are searched."""
    if not isinstance(text, str) or not text.strip():
        return None
    m = _PRICE_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def estimate_stop_cost(
    operator: str | None,
    power_kw: float,
    kwh_added: float,
    tarification_text: str | None = None,
) -> dict:
    parsed = parse_irve_tarification(tarification_text)
    if parsed is not None:
        return {
            "price_per_kwh": parsed,
            "total_eur": parsed * kwh_added,
            "source": "irve",
        }
    price, source = estimate_price_per_kwh(operator, power_kw)
    return {
        "price_per_kwh": price,
        "total_eur": price * kwh_added,
        "source": source,
    }
