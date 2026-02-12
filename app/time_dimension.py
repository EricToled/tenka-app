"""
time_dimension.py — Generación y mantenimiento de la dimensión tiempo (dim_tiempo).

La dimensión tiempo usa date_id = YYYYMM como clave primaria.
Cada registro representa un mes calendario completo.
"""

import calendar
from datetime import date

from sqlalchemy.orm import Session

from .models import DimTiempo

MONTH_NAMES = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}


def ensure_time_dimension(session: Session, start_year: int, end_year: int) -> int:
    """
    Crea registros en dim_tiempo para todos los meses desde start_year-01
    hasta end_year-12, solo si no existen.

    Args:
        session: Sesión de SQLAlchemy activa.
        start_year: Año de inicio (inclusive).
        end_year: Año de fin (inclusive).

    Returns:
        Cantidad de registros nuevos insertados.
    """
    inserted = 0

    for anio in range(start_year, end_year + 1):
        for mes in range(1, 13):
            date_id = anio * 100 + mes

            exists = session.get(DimTiempo, date_id)
            if exists:
                continue

            ultimo_dia = calendar.monthrange(anio, mes)[1]

            registro = DimTiempo(
                date_id=date_id,
                anio=anio,
                mes_numero=mes,
                mes_nombre=MONTH_NAMES[mes],
                fecha_inicial=date(anio, mes, 1),
                fecha_final=date(anio, mes, ultimo_dia),
            )
            session.add(registro)
            inserted += 1

    session.commit()
    return inserted
