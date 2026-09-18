import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.core.database_pool import db_pool

logger = logging.getLogger(__name__)

CENTS = Decimal("0.01")


class PropertyNotFound(Exception):
    """Raised when a property does not exist for the requesting tenant."""


def to_money(amount: Decimal) -> Decimal:
    """
    Round an exact Decimal to cents using half-up (the accounting convention).

    Rounding happens exactly once, at the presentation boundary, on a Decimal.
    Never on a binary float: ``round(1.005, 2)`` gives 1.0 because 1.005 is
    stored as 1.00499999..., which is where the "few cents off" reports came from.
    """
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


def local_month_bounds_utc(year: int, month: int, tz_name: str) -> Tuple[datetime, datetime]:
    """
    Return the [start, end) UTC instants that bound a calendar month
    *as experienced in the property's own timezone*.

    A property in Europe/Paris has its March start at 2024-03-01 00:00 Paris,
    which is 2024-02-29 23:00 UTC. The previous implementation used naive
    ``datetime(year, month, 1)`` boundaries (effectively UTC), so a check-in at
    2024-02-29 23:30 UTC (already 1 March in Paris) fell into February.
    """
    tz = ZoneInfo(tz_name)
    start_local = datetime(year, month, 1, tzinfo=tz)
    if month == 12:
        end_local = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end_local = datetime(year, month + 1, 1, tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


async def _get_property_timezone(session, property_id: str, tenant_id: str) -> str:
    row = (
        await session.execute(
            text(
                """
                SELECT timezone
                FROM properties
                WHERE id = :property_id AND tenant_id = :tenant_id
                """
            ),
            {"property_id": property_id, "tenant_id": tenant_id},
        )
    ).fetchone()
    if row is None:
        raise PropertyNotFound(property_id)
    return row.timezone or "UTC"


async def calculate_total_revenue(
    property_id: str,
    tenant_id: str,
    year: Optional[int] = None,
    month: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Aggregate revenue for one property belonging to one tenant.

    With ``year``/``month`` supplied, the period is the calendar month in the
    property's local timezone; otherwise all-time.

    Isolation: every query is filtered by *both* property_id and tenant_id.
    Property IDs are not globally unique (``prop-001`` exists for two tenants),
    so property_id alone is never an adequate key.
    """
    await db_pool.initialize()

    async with db_pool.get_session() as session:
        tz_name = await _get_property_timezone(session, property_id, tenant_id)

        params: Dict[str, Any] = {"property_id": property_id, "tenant_id": tenant_id}
        period_filter = ""
        period: Optional[str] = None

        if year is not None and month is not None:
            start_utc, end_utc = local_month_bounds_utc(year, month, tz_name)
            params.update({"start_utc": start_utc, "end_utc": end_utc})
            period_filter = "AND check_in_date >= :start_utc AND check_in_date < :end_utc"
            period = f"{year:04d}-{month:02d}"
            logger.debug(
                "Revenue window for %s/%s (%s): %s -> %s UTC",
                tenant_id, property_id, tz_name, start_utc, end_utc,
            )

        row = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        COALESCE(SUM(total_amount), 0)::NUMERIC(12, 3) AS total_revenue,
                        COUNT(*)                        AS reservation_count,
                        MIN(currency)                   AS currency,
                        COUNT(DISTINCT currency)        AS currency_count
                    FROM reservations
                    WHERE property_id = :property_id
                      AND tenant_id   = :tenant_id
                      {period_filter}
                    """
                ),
                params,
            )
        ).fetchone()

    if row.currency_count and row.currency_count > 1:
        # Summing across currencies would be meaningless; surface it instead.
        raise ValueError(f"Mixed currencies for {tenant_id}/{property_id}")

    # asyncpg returns NUMERIC as Decimal already; str() round-trip keeps it exact.
    total = Decimal(str(row.total_revenue))

    return {
        "property_id": property_id,
        "tenant_id": tenant_id,
        "timezone": tz_name,
        "period": period,
        # Exact value as stored (3 dp) and the cent-rounded presentation value,
        # both serialised as strings so no binary-float step is involved.
        "total_exact": str(total),
        "total": str(to_money(total)),
        "currency": row.currency or "USD",
        "count": int(row.reservation_count),
    }


async def calculate_monthly_revenue(
    property_id: str, tenant_id: str, month: int, year: int
) -> Decimal:
    """Convenience wrapper kept for callers that want just the number."""
    result = await calculate_total_revenue(property_id, tenant_id, year=year, month=month)
    return Decimal(result["total_exact"])
