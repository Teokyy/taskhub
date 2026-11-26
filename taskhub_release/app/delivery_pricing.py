"""Deterministic courier tariff rules for TaskHub IRL deliveries.

The caller must obtain route distance and weather from trusted providers. The
browser must never submit a final price or a weather multiplier.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

MINIMUM_FARE_RUB = 200
BASE_FARE_RUB = 200
PER_KM_RUB = 25

# One combined severity is selected for the route/time window. Multipliers are
# not stacked: icy rain must not multiply both rain and ice coefficients.
WEATHER_FACTORS = {
    "clear": 1.0,
    "rain": 1.15,
    "heavy_rain": 1.30,
    "snow": 1.40,
    "ice": 1.60,
    "strong_frost": 1.20,
}


@dataclass(frozen=True)
class DeliveryQuote:
    distance_km: float
    weather: str
    weather_factor: float
    base_rub: int
    distance_rub: int
    total_rub: int


def calculate_delivery_quote(distance_km: float, weather: str) -> DeliveryQuote:
    """Calculate a courier's guaranteed gross reward in whole rubles."""
    if not isinstance(distance_km, (int, float)) or not isfinite(distance_km):
        raise ValueError("Некорректное расстояние маршрута")
    if distance_km < 0 or distance_km > 500:
        raise ValueError("Расстояние должно быть от 0 до 500 км")
    if weather not in WEATHER_FACTORS:
        raise ValueError("Неизвестные погодные условия")
    base = BASE_FARE_RUB
    distance = int(round(distance_km * PER_KM_RUB))
    factor = WEATHER_FACTORS[weather]
    total = max(MINIMUM_FARE_RUB, int(round((base + distance) * factor)))
    return DeliveryQuote(
        distance_km=round(float(distance_km), 2),
        weather=weather,
        weather_factor=factor,
        base_rub=base,
        distance_rub=distance,
        total_rub=total,
    )
