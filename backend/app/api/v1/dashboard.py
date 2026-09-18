import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.auth import authenticate_request as get_current_user
from app.services.cache import get_revenue_summary
from app.services.reservations import PropertyNotFound

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str,
    year: Optional[int] = Query(None, ge=1970, le=2100, description="Reporting year (property-local)"),
    month: Optional[int] = Query(None, ge=1, le=12, description="Reporting month (property-local)"),
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Revenue summary for one of the caller's properties.

    Without ``year``/``month`` the figure is all-time; with both it is the
    calendar month in the property's own timezone.
    """
    # BUG FIX: the tenant used to fall back to the literal "default_tenant" when
    # missing. A request with no tenant must be refused, never silently pooled
    # with other tenant-less callers.
    tenant_id = getattr(current_user, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No tenant associated with user")

    if (year is None) != (month is None):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Provide both year and month, or neither")

    try:
        revenue_data = await get_revenue_summary(property_id, tenant_id, year=year, month=month)
    except PropertyNotFound:
        # Same response whether the property does not exist or belongs to
        # another tenant, so the API does not reveal other tenants' IDs.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except Exception as e:
        # BUG FIX: the service used to swallow DB failures and return
        # hard-coded sample totals. Fake financial data is worse than an error.
        logger.exception("Revenue lookup failed for %s/%s: %s", tenant_id, property_id, e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Revenue data temporarily unavailable")

    return {
        "property_id": revenue_data["property_id"],
        "tenant_id": revenue_data["tenant_id"],
        "timezone": revenue_data["timezone"],
        "period": revenue_data["period"],
        # BUG FIX: the total was passed through float(). The value is already
        # rounded half-up to cents on a Decimal, so the float below is exact to
        # the cent and the client's own rounding step becomes a no-op. The
        # string fields carry the authoritative value for any consumer that
        # can avoid binary floats altogether.
        "total_revenue": float(revenue_data["total"]),
        "total_revenue_str": revenue_data["total"],
        "total_revenue_exact": revenue_data["total_exact"],
        "currency": revenue_data["currency"],
        "reservations_count": revenue_data["count"],
    }
