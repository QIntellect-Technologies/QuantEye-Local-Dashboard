"""Canonical weekday numbering for QuantEye Local Dashboard.

Python datetime.weekday(): 0=Monday ... 6=Sunday.
This is the only numbering payroll, shifts, seeders, and reports may use.
SQLite strftime('%w') / weekday() uses 0=Sunday; convert with sqlite_sunday0_to_python().
JavaScript Date.getDay() uses 0=Sunday; convert with js_sunday0_to_python().
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Iterable, List, Sequence, Tuple

MONDAY = 0
TUESDAY = 1
WEDNESDAY = 2
THURSDAY = 3
FRIDAY = 4
SATURDAY = 5
SUNDAY = 6

WEEKDAY_LABELS = {
    MONDAY: "Monday",
    TUESDAY: "Tuesday",
    WEDNESDAY: "Wednesday",
    THURSDAY: "Thursday",
    FRIDAY: "Friday",
    SATURDAY: "Saturday",
    SUNDAY: "Sunday",
}

DEFAULT_WORKING_DAYS: Tuple[int, ...] = (
    MONDAY,
    TUESDAY,
    WEDNESDAY,
    THURSDAY,
    FRIDAY,
    SATURDAY,
)


class WorkingDaysError(ValueError):
    """Raised when working_days is missing, duplicated, or outside 0-6."""


def sqlite_sunday0_to_python(sqlite_w: int) -> int:
    """SQLite weekday('now') / strftime('%w'): 0=Sunday ... 6=Saturday."""
    n = int(sqlite_w)
    return (n - 1) % 7


def python_to_sqlite_sunday0(python_w: int) -> int:
    n = int(python_w)
    return (n + 1) % 7


def js_sunday0_to_python(js_day: int) -> int:
    return sqlite_sunday0_to_python(js_day)


def label(day: int) -> str:
    return WEEKDAY_LABELS[int(day)]


def parse_working_days(working_days: Any = None) -> List[int]:
    """Return a sorted unique list of Python weekdays (0-6). Never silently coerce."""
    if working_days is None:
        raise WorkingDaysError("working_days is required and must be a non-empty list of integers 0-6")
    if isinstance(working_days, str):
        text = working_days.strip()
        if not text:
            raise WorkingDaysError("working_days is required and must be a non-empty list of integers 0-6")
        if text.startswith("["):
            import json
            try:
                working_days = json.loads(text)
            except (TypeError, ValueError) as exc:
                raise WorkingDaysError("working_days must be a JSON list of integers 0-6") from exc
        else:
            working_days = [part.strip() for part in text.split(",") if part.strip()]
    if not isinstance(working_days, (list, tuple)):
        raise WorkingDaysError("working_days must be a non-empty list of integers 0-6")
    if len(working_days) == 0:
        raise WorkingDaysError("working_days must be a non-empty list of integers 0-6")
    parsed: List[int] = []
    seen = set()
    for item in working_days:
        try:
            day = int(item)
        except (TypeError, ValueError, OverflowError) as exc:
            raise WorkingDaysError("working_days must contain only integers 0-6 (Monday=0 ... Sunday=6)") from exc
        if day < 0 or day > 6:
            raise WorkingDaysError(
                f"working_days value {day} is outside 0-6 (Monday=0 ... Sunday=6)"
            )
        if day in seen:
            raise WorkingDaysError(f"working_days contains duplicate weekday {day} ({label(day)})")
        seen.add(day)
        parsed.append(day)
    return sorted(parsed)


def default_working_days() -> List[int]:
    return list(DEFAULT_WORKING_DAYS)


def is_working_weekday(day: date, working_days: Sequence[int] | None = None) -> bool:
    days = parse_working_days(working_days if working_days is not None else default_working_days())
    return day.weekday() in days


def dates_in_range(
    start: date,
    end: date,
    working_days: Sequence[int] | None = None,
) -> List[date]:
    days = set(parse_working_days(working_days if working_days is not None else default_working_days()))
    out: List[date] = []
    current = start
    while current <= end:
        if current.weekday() in days:
            out.append(current)
        current += timedelta(days=1)
    return out


def month_date_range(year: int, month: int) -> Tuple[date, date]:
    import calendar
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def scheduled_days_in_month(
    year: int,
    month: int,
    working_days: Sequence[int] | None = None,
    holidays: Iterable[date] | None = None,
    join_on: date | None = None,
    exit_on: date | None = None,
) -> List[date]:
    start, end = month_date_range(year, month)
    if join_on:
        start = max(start, join_on)
    if exit_on:
        end = min(end, exit_on)
    if start > end:
        return []
    skip = {d for d in (holidays or ())}
    return [d for d in dates_in_range(start, end, working_days) if d not in skip]
