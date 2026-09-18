"""
Regression tests for the three reported dashboard bugs.

These cover the pure logic (no database / Redis needed) so they run anywhere:
    cd backend && python -m pytest -q
"""
from datetime import datetime, timezone
from decimal import Decimal

from app.services.cache import revenue_cache_key
from app.services.reservations import local_month_bounds_utc, to_money


# --- Bug 2: cross-tenant cache leak -------------------------------------------

def test_cache_key_is_tenant_scoped():
    a = revenue_cache_key("tenant-a", "prop-001")
    b = revenue_cache_key("tenant-b", "prop-001")
    assert a != b
    assert "tenant-a" in a and "tenant-b" in b


def test_cache_key_includes_period():
    assert revenue_cache_key("tenant-a", "prop-001") != revenue_cache_key("tenant-a", "prop-001", 2024, 3)
    assert revenue_cache_key("tenant-a", "prop-001", 2024, 3) == "revenue:tenant-a:prop-001:2024-03"


# --- Bug 1: month boundaries must follow the property's local timezone --------

def test_paris_march_starts_before_utc_march():
    start, end = local_month_bounds_utc(2024, 3, "Europe/Paris")
    assert start == datetime(2024, 2, 29, 23, 0, tzinfo=timezone.utc)
    # DST began 31 March 2024 in Paris, so the end boundary is UTC+2.
    assert end == datetime(2024, 3, 31, 22, 0, tzinfo=timezone.utc)


def test_seed_reservation_falls_in_paris_march():
    checkin = datetime(2024, 2, 29, 23, 30, tzinfo=timezone.utc)  # res-tz-1
    start, end = local_month_bounds_utc(2024, 3, "Europe/Paris")
    assert start <= checkin < end
    # ...but not in a naive/UTC March, which is what the old code computed.
    assert not (datetime(2024, 3, 1, tzinfo=timezone.utc) <= checkin)


def test_new_york_march_starts_after_utc_march():
    start, _ = local_month_bounds_utc(2024, 3, "America/New_York")
    assert start == datetime(2024, 3, 1, 5, 0, tzinfo=timezone.utc)


def test_december_rolls_into_next_year():
    start, end = local_month_bounds_utc(2024, 12, "UTC")
    assert start == datetime(2024, 12, 1, tzinfo=timezone.utc)
    assert end == datetime(2025, 1, 1, tzinfo=timezone.utc)


# --- Bug 3: money rounding ------------------------------------------------------

def test_seed_amounts_sum_exactly():
    amounts = [Decimal("1250.000"), Decimal("333.333"), Decimal("333.333"), Decimal("333.334")]
    assert sum(amounts) == Decimal("2250.000")
    assert to_money(sum(amounts)) == Decimal("2250.00")


def test_half_up_rounding_not_bankers_or_float():
    assert to_money(Decimal("1.005")) == Decimal("1.01")   # float round(1.005, 2) -> 1.0
    assert to_money(Decimal("1.004")) == Decimal("1.00")
    assert to_money(Decimal("0.125")) == Decimal("0.13")   # ROUND_HALF_EVEN would give 0.12
    assert to_money(Decimal("333.333")) == Decimal("333.33")
