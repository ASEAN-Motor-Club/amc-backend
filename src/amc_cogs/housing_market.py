from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Avg
from django.utils import timezone

from amc.models import ServerStatus

logger = logging.getLogger("amc.cogs.housing_market")

ZERO_GUID = "00000000000000000000000000000000"
SECONDS_PER_DAY = 86400
CACHE_KEY = "housing_market_multiplier"


async def get_market_multiplier(
    houses: list[dict],
    max_rent_days: int,
    *,
    cache_timeout: int | None = None,
) -> tuple[float, dict]:
    """Compute market multiplier from player activity + rent health.

    Returns (multiplier, breakdown) where breakdown has the individual
    factors for display.
    """
    if cache_timeout is None:
        cache_timeout = settings.HOUSING_MARKET_CACHE_SECONDS

    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached["multiplier"], cached

    ref_players = settings.HOUSING_MARKET_REFERENCE_PLAYERS
    pf_min = settings.HOUSING_MARKET_PLAYER_FACTOR_MIN
    pf_max = settings.HOUSING_MARKET_PLAYER_FACTOR_MAX
    rh_min = settings.HOUSING_MARKET_RENT_HEALTH_MIN
    rh_max = settings.HOUSING_MARKET_RENT_HEALTH_MAX
    m_min = settings.HOUSING_MARKET_MIN_MULTIPLIER
    m_max = settings.HOUSING_MARKET_MAX_MULTIPLIER

    seven_days_ago = timezone.now() - timedelta(days=7)
    avg_result = await ServerStatus.objects.filter(
        timestamp__gte=seven_days_ago
    ).aaggregate(avg=Avg("num_players"))
    avg_players = avg_result["avg"] or 0.0

    player_factor = max(pf_min, min(pf_max, avg_players / ref_players)) if ref_players else 1.0

    rented = [
        h for h in houses
        if h.get("Net_OwnerCharacterGuid", "") not in ("", ZERO_GUID)
    ]
    if rented and max_rent_days > 0:
        max_seconds = max_rent_days * SECONDS_PER_DAY
        total_pct = sum(
            h.get("Net_RentLeftTimeSeconds", 0) / max_seconds for h in rented
        )
        avg_rent_pct = total_pct / len(rented)
    else:
        avg_rent_pct = 1.0

    rent_health = max(rh_min, min(rh_max, avg_rent_pct))

    raw_multiplier = player_factor * rent_health
    market_multiplier = max(m_min, min(m_max, raw_multiplier))

    breakdown = {
        "avg_players": round(avg_players, 1),
        "player_factor": round(player_factor, 2),
        "avg_rent_pct": round(avg_rent_pct * 100, 1),
        "rent_health": round(rent_health, 2),
        "multiplier": round(market_multiplier, 2),
    }

    cache.set(CACHE_KEY, breakdown, cache_timeout)

    return market_multiplier, breakdown
