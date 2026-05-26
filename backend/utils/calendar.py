# liquidity_calendar.py
# Central place for "low liquidity" calendar logic (ES-focused).
# Trading day is based on America/New_York.

from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional, TypedDict

try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


NY_TZ_NAME = "America/New_York"


# 2026 Low-liquidity / thin-participation days for ES context.
# Keys: YYYY-MM-DD (NY date)
# Values: short reason string shown in UI.

LOW_LIQUIDITY_DAYS: Dict[str, str] = {

    # ======================
    # 2026
    # ======================

    "2026-01-02": "First trading day after New Year (often thin)",
    "2026-01-16": "Day before MLK weekend (often thinner)",
    "2026-01-19": "MLK Day (holiday / closed)",

    "2026-02-13": "Pre–Presidents’ Day weekend (often thinner)",
    "2026-02-16": "Presidents’ Day (holiday / closed)",

    "2026-04-02": "Day before Good Friday (often thin)",
    "2026-04-03": "Good Friday (holiday / closed)",

    "2026-05-22": "Pre–Memorial Day weekend (often thin)",
    "2026-05-25": "Memorial Day (holiday / closed)",

    "2026-06-18": "Day before Juneteenth (often thinner)",
    "2026-06-19": "Juneteenth (holiday / closed)",

    "2026-07-02": "Day before Independence Day observed (often very thin)",
    "2026-07-03": "Independence Day observed (holiday / closed)",

    "2026-09-04": "Day before Labor Day weekend (often thin)",
    "2026-09-07": "Labor Day (holiday / closed)",

    "2026-11-25": "Day before Thanksgiving (often thin)",
    "2026-11-26": "Thanksgiving (holiday / closed)",
    "2026-11-27": "Day after Thanksgiving (early close / often thin)",

    "2026-12-24": "Christmas Eve (early close / often thin)",
    "2026-12-25": "Christmas Day (holiday / closed)",
    "2026-12-28": "Post-Christmas week (often thin)",
    "2026-12-29": "Thin holiday week",
    "2026-12-30": "Thin holiday week",
    "2026-12-31": "Year-end positioning (often thin)",


    # ======================
    # 2027
    # ======================

    "2027-01-04": "First trading days of year (often thin)",
    "2027-01-15": "Day before MLK weekend (often thinner)",
    "2027-01-18": "MLK Day (holiday / closed)",

    "2027-02-12": "Pre–Presidents’ Day weekend (often thinner)",
    "2027-02-15": "Presidents’ Day (holiday / closed)",

    "2027-03-25": "Day before Good Friday (often thin)",
    "2027-03-26": "Good Friday (holiday / closed)",

    "2027-05-28": "Pre–Memorial Day weekend (often thin)",
    "2027-05-31": "Memorial Day (holiday / closed)",

    "2027-06-18": "Juneteenth observed (holiday / closed)",

    "2027-07-02": "Day before Independence Day observed (often thin)",
    "2027-07-05": "Independence Day observed (holiday / closed)",

    "2027-09-03": "Day before Labor Day weekend (often thin)",
    "2027-09-06": "Labor Day (holiday / closed)",

    "2027-11-24": "Day before Thanksgiving (often thin)",
    "2027-11-25": "Thanksgiving (holiday / closed)",
    "2027-11-26": "Day after Thanksgiving (early close / often thin)",

    "2027-12-24": "Christmas Eve (early close / often thin)",
    "2027-12-27": "Christmas observed (holiday / closed)",
    "2027-12-28": "Post-Christmas week (often thin)",
    "2027-12-29": "Thin holiday week",
    "2027-12-30": "Thin holiday week",
    "2027-12-31": "Year-end positioning (often thin)",


    # ======================
    # 2028
    # ======================

    "2028-01-03": "First trading days of year (often thin)",
    "2028-01-14": "Day before MLK weekend (often thinner)",
    "2028-01-17": "MLK Day (holiday / closed)",

    "2028-02-18": "Pre–Presidents’ Day weekend (often thinner)",
    "2028-02-21": "Presidents’ Day (holiday / closed)",

    "2028-04-13": "Day before Good Friday (often thin)",
    "2028-04-14": "Good Friday (holiday / closed)",

    "2028-05-26": "Pre–Memorial Day weekend (often thin)",
    "2028-05-29": "Memorial Day (holiday / closed)",

    "2028-06-19": "Juneteenth (holiday / closed)",

    "2028-07-03": "Day before Independence Day (often thin)",
    "2028-07-04": "Independence Day (holiday / closed)",

    "2028-09-01": "Day before Labor Day weekend (often thin)",
    "2028-09-04": "Labor Day (holiday / closed)",

    "2028-11-22": "Day before Thanksgiving (often thin)",
    "2028-11-23": "Thanksgiving (holiday / closed)",
    "2028-11-24": "Day after Thanksgiving (early close / often thin)",

    "2028-12-22": "Pre-Christmas positioning (often thin)",
    "2028-12-25": "Christmas Day (holiday / closed)",
    "2028-12-26": "Post-Christmas week (often thin)",
    "2028-12-27": "Thin holiday week",
    "2028-12-28": "Thin holiday week",
    "2028-12-29": "Year-end positioning (often thin)",
}



class TodayInfo(TypedDict):
    date: str                 # YYYY-MM-DD in NY
    low_liquidity: bool
    reason: Optional[str]


def _now_ny() -> datetime:
    """
    Returns current time in America/New_York.
    If zoneinfo isn't available, falls back to local time (still returns a datetime).
    """
    if ZoneInfo is None:
        return datetime.now()
    return datetime.now(ZoneInfo(NY_TZ_NAME))


def ny_date_str(dt: Optional[datetime] = None) -> str:
    """
    Returns NY date as YYYY-MM-DD (string).
    If dt provided: converts to NY if timezone-aware; if naive, treats as already local.
    """
    if dt is None:
        dt = _now_ny()

    # If aware and zoneinfo available, normalize to NY
    if getattr(dt, "tzinfo", None) is not None and ZoneInfo is not None:
        dt = dt.astimezone(ZoneInfo(NY_TZ_NAME))

    return dt.strftime("%Y-%m-%d")


def is_low_liquidity(date_yyyy_mm_dd: str) -> TodayInfo:
    """
    Pure function: given YYYY-MM-DD, returns low-liquidity info.
    """
    reason = LOW_LIQUIDITY_DAYS.get(date_yyyy_mm_dd)
    return TodayInfo(
        date=date_yyyy_mm_dd,
        low_liquidity=bool(reason),
        reason=reason,
    )


def get_today_info() -> TodayInfo:
    """
    Convenience wrapper for "today" in NY.
    """
    d = ny_date_str()
    return is_low_liquidity(d)
