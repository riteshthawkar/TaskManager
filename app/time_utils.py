from __future__ import annotations

import os
import logging
from datetime import date, datetime, time, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("taskmanager.time")

APP_TIMEZONE_NAME = os.getenv("APP_TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata"

try:
    APP_TIMEZONE = ZoneInfo(APP_TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    logger.warning("Unknown APP_TIMEZONE=%r. Falling back to UTC.", APP_TIMEZONE_NAME)
    APP_TIMEZONE = timezone.utc
    APP_TIMEZONE_NAME = "UTC"


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def local_now() -> datetime:
    return datetime.now(APP_TIMEZONE)


def local_today() -> date:
    return local_now().date()


def local_today_str() -> str:
    return local_today().strftime("%Y-%m-%d")


def utc_naive_to_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return aware.astimezone(APP_TIMEZONE)


def local_datetime_from_input(value: str, default_time: time | None = None) -> datetime | None:
    if not value:
        return None

    cleaned = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue

    parsed_date = local_date_from_input(cleaned)
    if parsed_date is None:
        return None
    return datetime.combine(parsed_date, default_time or time(23, 59, 59))


def local_datetime_input_to_utc_naive(value: str, default_time: time | None = None) -> datetime | None:
    parsed = local_datetime_from_input(value, default_time=default_time)
    if parsed is None:
        return None
    local_value = parsed.replace(tzinfo=APP_TIMEZONE)
    return local_value.astimezone(timezone.utc).replace(tzinfo=None)


def local_datetime_input_value(value: datetime | None) -> str:
    local_value = utc_naive_to_local(value)
    return local_value.strftime("%Y-%m-%dT%H:%M") if local_value else ""


def local_datetime_input_display(value: str | None) -> str:
    parsed = local_datetime_from_input(value or "")
    return parsed.strftime("%b %d, %Y %I:%M %p") if parsed else ""


def local_date_input_to_utc_naive_end_of_day(value: str) -> datetime | None:
    return local_datetime_input_to_utc_naive(value, default_time=time(23, 59, 59))


def local_date_input_value(value: datetime | None) -> str:
    local_value = utc_naive_to_local(value)
    return local_value.date().isoformat() if local_value else ""


def local_date_from_input(value: str) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def local_date_to_utc_naive_end_of_day(value: date | None) -> datetime | None:
    if value is None:
        return None
    local_deadline = datetime.combine(value, time(23, 59, 59), tzinfo=APP_TIMEZONE)
    return local_deadline.astimezone(timezone.utc).replace(tzinfo=None)


def shift_utc_naive_by_local_days(value: datetime | None, days: int) -> datetime | None:
    if value is None:
        return None
    local_value = utc_naive_to_local(value)
    if local_value is None:
        return None
    shifted_local = local_value + timedelta(days=days)
    return shifted_local.astimezone(timezone.utc).replace(tzinfo=None)
