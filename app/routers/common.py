"""Small helpers shared by routers."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from fastapi import Query
from sqlalchemy.orm import Session

from app.schemas.common import ErrorOut
from app.services import settings as settings_svc
from app.timeutil import day_range_utc, get_zone, today_in

DateFrom = Annotated[date | None, Query(description="Fecha inicial YYYY-MM-DD (zona horaria del negocio). Por defecto hoy.")]
DateTo = Annotated[date | None, Query(description="Fecha final YYYY-MM-DD inclusive. Por defecto = date_from.")]


def resolve_dates(db: Session, date_from: date | None, date_to: date | None) -> tuple[date, date, str]:
    tz_name = settings_svc.get_row(db).timezone
    tz = get_zone(tz_name)
    d1 = date_from or today_in(tz)
    d2 = date_to or d1
    day_range_utc(d1, d2, tz)  # validates range
    return d1, d2, tz_name


def optional_range(
    db: Session, date_from: date | None, date_to: date | None
) -> tuple[datetime | None, datetime | None]:
    if date_from is None and date_to is None:
        return None, None
    tz = get_zone(settings_svc.get_row(db).timezone)
    d1 = date_from or date_to
    d2 = date_to or date_from
    assert d1 is not None and d2 is not None
    return day_range_utc(d1, d2, tz)


ERR: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorOut, "description": "Solicitud inválida"},
    401: {"model": ErrorOut, "description": "No autenticado"},
    403: {"model": ErrorOut, "description": "Sin permiso"},
    404: {"model": ErrorOut, "description": "No encontrado"},
    409: {"model": ErrorOut, "description": "Conflicto de estado de negocio"},
    422: {"model": ErrorOut, "description": "Datos inválidos / regla de negocio"},
}
