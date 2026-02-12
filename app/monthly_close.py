"""
monthly_close.py — Lógica de cierre mensual.

Regla de negocio: ventana rolling de 12 meses históricos.
    - Al cerrar el mes M, se eliminan los datos del mes M-12.
    - Se verifica que cada tabla histórica mantenga exactamente 12 date_id distintos.
    - Se registra el resultado en control_cierre_mensual.

Tablas históricas afectadas:
    fact_sales_in, fact_sales_out, fact_stock_cliente,
    fact_inventario_interno, fact_inventario_transito.

fact_forecast_sales NO se purga en el cierre mensual (tiene su propia lógica).
"""

import logging
from datetime import datetime

from sqlalchemy import distinct, func
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import (
    ControlCierreMensual,
    FactDiasInventarioHistorico,
    FactForecastSales,
    FactInventarioInterno,
    FactInventarioTransito,
    FactSalesIn,
    FactSalesOut,
    FactStockCliente,
)

logger = logging.getLogger(__name__)

# Tablas históricas que participan en el rolling de 12 meses
# Incluye fact_dias_inventario_historico (Fase 2) para mantener coherencia
FACT_TABLES = [
    FactSalesIn,
    FactSalesOut,
    FactStockCliente,
    FactInventarioInterno,
    FactInventarioTransito,
    FactDiasInventarioHistorico,
]


def _compute_month_minus_n(date_id: int, n: int) -> int:
    """
    Retrocede n meses desde un date_id (YYYYMM).
    Maneja correctamente el cambio de año.

    Ejemplo: _compute_month_minus_n(202501, 12) → 202401
             _compute_month_minus_n(202503, 2)  → 202501
             _compute_month_minus_n(202401, 1)  → 202312
    """
    anio = date_id // 100
    mes = date_id % 100

    total_meses = anio * 12 + (mes - 1) - n
    nuevo_anio = total_meses // 12
    nuevo_mes = (total_meses % 12) + 1

    return nuevo_anio * 100 + nuevo_mes


def run_monthly_close(session: Session, mes_cierre_date_id: int) -> dict:
    """
    Ejecuta el cierre mensual para el mes indicado.

    Pre-condiciones:
        - Los datos del mes de cierre YA fueron cargados en las tablas de hechos.
        - dim_tiempo ya contiene el date_id del mes de cierre.

    Pasos:
        1. Registrar inicio en control_cierre_mensual (estado = EN_PROCESO).
        2. Calcular mes_antiguo = mes_cierre - 12.
        3. Eliminar registros del mes_antiguo en cada tabla histórica.
        4. Verificar que cada tabla tenga exactamente 12 date_id distintos.
        5. Actualizar control_cierre_mensual con resultado final.

    Args:
        session: Sesión de SQLAlchemy activa.
        mes_cierre_date_id: date_id del mes que se cierra (YYYYMM).

    Returns:
        Diccionario con resultado del cierre:
            {estado, mensaje, deleted_counts, distinct_counts}

    Ejemplo:
        Si mes_cierre_date_id = 202507 (julio 2025):
        - Se eliminan registros con date_id = 202407 (mes M-12).
        - La ventana histórica resultante queda: 202408 → 202507 (12 meses).
        - fact_forecast_sales se verifica pero NO se purga.
    """
    fecha_inicio = datetime.now()

    # 1. Registrar inicio
    control = ControlCierreMensual(
        mes_cierre_date_id=mes_cierre_date_id,
        estado="EN_PROCESO",
        mensaje="Cierre mensual iniciado.",
        fecha_inicio=fecha_inicio,
    )
    session.add(control)
    session.flush()

    try:
        # 2. Calcular mes antiguo a eliminar
        mes_antiguo = _compute_month_minus_n(mes_cierre_date_id, 12)
        logger.info(
            "Cierre mes %d: eliminando datos del mes %d",
            mes_cierre_date_id,
            mes_antiguo,
        )

        # 3. Eliminar registros del mes antiguo
        deleted_counts = {}
        for model in FACT_TABLES:
            deleted = (
                session.query(model)
                .filter(model.date_id == mes_antiguo)
                .delete(synchronize_session="fetch")
            )
            deleted_counts[model.__tablename__] = deleted
            logger.info(
                "  %s: %d registros eliminados para date_id=%d",
                model.__tablename__,
                deleted,
                mes_antiguo,
            )

        # 4. Verificar conteo de date_id distintos
        distinct_counts = {}
        warnings = []
        for model in FACT_TABLES:
            count = session.query(
                func.count(distinct(model.date_id))
            ).scalar()
            distinct_counts[model.__tablename__] = count

            if count != 12:
                warnings.append(
                    f"{model.__tablename__}: {count} date_id distintos (esperados: 12)"
                )

        # 4b. Verificar que fact_forecast_sales tenga 12 date_id proyectados
        #     (solo lectura — no se eliminan datos de forecast en el cierre)
        forecast_distinct = session.query(
            func.count(distinct(FactForecastSales.date_id))
        ).scalar()
        distinct_counts["fact_forecast_sales"] = forecast_distinct
        if forecast_distinct != 12:
            warnings.append(
                f"fact_forecast_sales: {forecast_distinct} date_id distintos (esperados: 12)"
            )

        # 5. Determinar estado final
        if warnings:
            estado = "COMPLETADO"  # Completado pero con advertencias
            mensaje = (
                f"Cierre mes {mes_cierre_date_id} completado con advertencias. "
                f"Mes eliminado: {mes_antiguo}. "
                f"Eliminados: {deleted_counts}. "
                f"Advertencias: {'; '.join(warnings)}"
            )
            logger.warning(mensaje)
        else:
            estado = "COMPLETADO"
            mensaje = (
                f"Cierre mes {mes_cierre_date_id} exitoso. "
                f"Mes eliminado: {mes_antiguo}. "
                f"Eliminados: {deleted_counts}. "
                f"Todas las tablas con 12 date_id distintos."
            )
            logger.info(mensaje)

        # Actualizar registro de control
        control.estado = estado
        control.mensaje = mensaje
        control.fecha_fin = datetime.now()

        session.commit()

        return {
            "estado": estado,
            "mensaje": mensaje,
            "deleted_counts": deleted_counts,
            "distinct_counts": distinct_counts,
        }

    except Exception as e:
        session.rollback()

        # Registrar error en control (nueva sesión para no perder el registro)
        error_session = SessionLocal()
        try:
            error_control = ControlCierreMensual(
                mes_cierre_date_id=mes_cierre_date_id,
                estado="ERROR",
                mensaje=f"Error en cierre: {str(e)}",
                fecha_inicio=fecha_inicio,
                fecha_fin=datetime.now(),
            )
            error_session.add(error_control)
            error_session.commit()
        finally:
            error_session.close()

        logger.exception("Error en cierre mensual para %d", mes_cierre_date_id)
        raise


def get_historical_window(mes_cierre_date_id: int) -> tuple[int, int]:
    """
    Calcula la ventana histórica de 12 meses para un mes de cierre dado.

    Args:
        mes_cierre_date_id: date_id del mes de cierre (YYYYMM).

    Returns:
        Tupla (date_id_inicio, date_id_fin) de la ventana histórica.

    Ejemplo:
        get_historical_window(202507) → (202408, 202507)
        get_historical_window(202601) → (202502, 202601)
    """
    date_id_inicio = _compute_month_minus_n(mes_cierre_date_id, 11)
    return (date_id_inicio, mes_cierre_date_id)
