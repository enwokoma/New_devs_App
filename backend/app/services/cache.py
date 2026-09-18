import json
import os
from typing import Any, Dict, Optional

import redis.asyncio as redis

# Initialize Redis client (typically configured centrally).
redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))

CACHE_TTL_SECONDS = 300


def revenue_cache_key(
    tenant_id: str, property_id: str, year: Optional[int] = None, month: Optional[int] = None
) -> str:
    """
    Tenant-scoped cache key.

    BUG FIX: the key used to be ``revenue:{property_id}``. Property IDs are only
    unique *within* a tenant (both Sunset and Ocean own a ``prop-001``), so the
    first tenant to warm the cache had its figures served to the other tenant
    for the next five minutes. That is a cross-tenant data leak, not just a
    stale-cache bug. Every dimension that changes the answer must be in the key.
    """
    period = f"{year:04d}-{month:02d}" if (year is not None and month is not None) else "all"
    return f"revenue:{tenant_id}:{property_id}:{period}"


async def get_revenue_summary(
    property_id: str,
    tenant_id: str,
    year: Optional[int] = None,
    month: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Fetches revenue summary, utilizing caching to improve performance.
    """
    cache_key = revenue_cache_key(tenant_id, property_id, year, month)

    cached = await redis_client.get(cache_key)
    if cached:
        data = json.loads(cached)
        # Defensive check: never hand back an entry that belongs to someone else.
        if data.get("tenant_id") == tenant_id and data.get("property_id") == property_id:
            return data
        await redis_client.delete(cache_key)

    # Revenue calculation is delegated to the reservation service.
    from app.services.reservations import calculate_total_revenue

    result = await calculate_total_revenue(property_id, tenant_id, year=year, month=month)

    await redis_client.setex(cache_key, CACHE_TTL_SECONDS, json.dumps(result))

    return result
