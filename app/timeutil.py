from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.errors import unprocessable


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise unprocessable("invalid_timezone", f"Zona horaria inválida: {name}")


def today_in(tz: ZoneInfo) -> date:
    return datetime.now(timezone.utc).astimezone(tz).date()


def day_range_utc(date_from: date, date_to: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """[start, end) in UTC covering business days date_from..date_to inclusive."""
    if date_to < date_from:
        raise unprocessable("invalid_date_range", "date_to debe ser mayor o igual que date_from")
    if (date_to - date_from).days > 366:
        raise unprocessable("invalid_date_range", "El rango máximo es de 366 días")
    start = datetime.combine(date_from, time.min, tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
    return start, end
