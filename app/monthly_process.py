"""
monthly_process.py — Orquestador del proceso mensual completo.

Seccion 12: Proceso mensual completo + re-ejecucion automatica de Unconstrained Demand.

Pasos:
    1) Calcular e insertar inventario final en cliente y DOS del mes de cierre.
    2) Ejecutar el cierre mensual (rolling 12) en tablas historicas.
    3) Reconstruir datos de DOS historicos (fact_dias_inventario_historico).
    4) Determinar cliente-SKU elegibles para proyeccion.
    4.5) Detectar clientes/SKUs nuevos en forecast.
    5) Proyectar Sales Out Unconstrained.
    6) Calcular objetivos de DOS para los 12 meses proyectados.
    7) Proyectar Sales In Unconstrained e inventarios con el algoritmo iterativo.
    7.5) Proyectar clientes nuevos registrados (basado en forecast).
    8) Retornar resumen.
"""

import logging
from datetime import datetime

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from .eligibility import get_eligible_client_skus
from .historical_dos import build_historical_dos
from .models import (
    DimCliente,
    DimSku,
    FactDiasInventarioHistorico,
    FactForecastSales,
    FactSalesIn,
    FactSalesOut,
    FactStockCliente,
    RptForecastSinCatalogo,
)
from .eligibility import _generate_date_id_range
from .monthly_close import _compute_month_minus_n, run_monthly_close
from .new_client_detection import (
    detect_and_report_new_client_skus,
    project_new_client_skus,
)
from .unconstrained_demand import (
    project_sales_in_and_inventory_unc,
    project_sales_out_unconstrained,
)

logger = logging.getLogger(__name__)


def _compute_closing_month_inventory(
    session: Session,
    mes_cierre_date_id: int,
) -> int:
    """
    Seccion 11, paso 2:
    Calcula inventario final en cliente del mes de cierre y su DOS,
    e inserta registros en fact_dias_inventario_historico.

    Formula:
        inv_final_cierre = inv_final_anterior + sales_in_cierre - sales_out_cierre

    Para cada cliente-SKU con actividad en el mes de cierre.

    Returns:
        Numero de registros de DOS insertados para el mes de cierre.
    """
    mes_anterior = _compute_month_minus_n(mes_cierre_date_id, 1)

    # Obtener pares cliente-SKU con actividad en el mes de cierre
    # (presentes en Sales In o Sales Out del mes)
    pairs_in = (
        session.query(FactSalesIn.cliente_id, FactSalesIn.sku_id)
        .filter(FactSalesIn.date_id == mes_cierre_date_id)
        .distinct()
        .all()
    )
    pairs_out = (
        session.query(FactSalesOut.cliente_id, FactSalesOut.sku_id)
        .filter(FactSalesOut.date_id == mes_cierre_date_id)
        .distinct()
        .all()
    )
    all_pairs = set()
    all_pairs.update((r.cliente_id, r.sku_id) for r in pairs_in)
    all_pairs.update((r.cliente_id, r.sku_id) for r in pairs_out)

    if not all_pairs:
        logger.warning("No hay actividad de Sales In/Out para el mes %d", mes_cierre_date_id)
        return 0

    inserted = 0

    for cliente_id, sku_id in all_pairs:
        # Inventario final del mes anterior
        inv_anterior = (
            session.query(FactStockCliente.inventario_final_unidades)
            .filter(
                FactStockCliente.cliente_id == cliente_id,
                FactStockCliente.sku_id == sku_id,
                FactStockCliente.date_id == mes_anterior,
            )
            .scalar()
        ) or 0

        # Sales In del mes de cierre
        si_cierre = (
            session.query(func.sum(FactSalesIn.unidades_sales_in))
            .filter(
                FactSalesIn.cliente_id == cliente_id,
                FactSalesIn.sku_id == sku_id,
                FactSalesIn.date_id == mes_cierre_date_id,
            )
            .scalar()
        ) or 0

        # Sales Out del mes de cierre
        so_cierre = (
            session.query(func.sum(FactSalesOut.unidades_sales_out))
            .filter(
                FactSalesOut.cliente_id == cliente_id,
                FactSalesOut.sku_id == sku_id,
                FactSalesOut.date_id == mes_cierre_date_id,
            )
            .scalar()
        ) or 0

        inv_final_cierre = int(inv_anterior) + int(si_cierre) - int(so_cierre)
        if inv_final_cierre < 0:
            inv_final_cierre = 0

        # Calcular promedio ventas Sales Out de los N meses anteriores al cierre
        # FIX R5: N = min(12, meses desde first_sale_date), NOT hardcoded 12
        prev_12_start = _compute_month_minus_n(mes_cierre_date_id, 12)
        prev_12_end = _compute_month_minus_n(mes_cierre_date_id, 1)
        prev_ids = _generate_date_id_range(prev_12_start, prev_12_end)

        # Get first_sale_date for this client-SKU pair
        first_sale = (
            session.query(func.min(FactSalesOut.date_id))
            .filter(
                FactSalesOut.cliente_id == cliente_id,
                FactSalesOut.sku_id == sku_id,
            )
            .scalar()
        )
        if first_sale and first_sale > prev_12_start:
            prev_ids = [d for d in prev_ids if d >= first_sale]

        n_months = len(prev_ids)

        sum_ventas = (
            session.query(
                func.sum(FactSalesOut.unidades_sales_out)
            )
            .filter(
                FactSalesOut.cliente_id == cliente_id,
                FactSalesOut.sku_id == sku_id,
                FactSalesOut.date_id.in_(prev_ids),
            )
            .scalar()
        ) if n_months > 0 else None

        avg_ventas = float(sum_ventas or 0) / n_months if n_months > 0 else 0.0

        if avg_ventas > 0:
            mos = inv_final_cierre / avg_ventas
            dos = mos * 30
        else:
            mos = None
            dos = None

        # Insertar DOS historico del mes de cierre
        registro = FactDiasInventarioHistorico(
            date_id=mes_cierre_date_id,
            cliente_id=cliente_id,
            sku_id=sku_id,
            inventario_final=inv_final_cierre,
            promedio_ventas_12m=avg_ventas if avg_ventas > 0 else None,
            months_of_sale_historico=mos,
            days_of_sale_historico=dos,
        )
        session.add(registro)
        inserted += 1

    session.flush()
    logger.info(
        "_compute_closing_month_inventory: %d registros DOS para mes %d",
        inserted,
        mes_cierre_date_id,
    )
    return inserted


def run_monthly_process(session: Session, mes_cierre_date_id: int) -> dict:
    """
    Proceso completo de cierre mensual + recalculo de Unconstrained Demand.

    Supone que:
        - Los archivos del mes de cierre ya fueron subidos y cargados
          mediante los endpoints de upload (Sales In, Sales Out, inventario interno,
          inventario en transito).

    Pasos:
        1) Calcular e insertar inventario final en cliente y DOS del mes de cierre.
        2) Ejecutar el cierre mensual (rolling 12) en tablas historicas.
        3) Reconstruir datos de DOS historicos (fact_dias_inventario_historico).
        4) Determinar cliente-SKU elegibles para proyeccion.
        4.5) Detectar clientes/SKUs nuevos en forecast.
        5) Proyectar Sales Out Unconstrained.
        6) (Objetivos de DOS se calculan internamente en paso 7)
        7) Proyectar Sales In Unconstrained e inventarios con algoritmo iterativo.
        7.5) Proyectar clientes nuevos registrados desde forecast.
        8) Retornar resumen.

    Args:
        session: Sesion de SQLAlchemy activa.
        mes_cierre_date_id: date_id del mes de cierre (YYYYMM).

    Returns:
        Diccionario con resumen completo del proceso.
    """
    start_time = datetime.now()
    summary = {
        "mes_cierre_date_id": mes_cierre_date_id,
        "estado": "EN_PROCESO",
        "pasos": {},
    }

    try:
        # Paso 1: Inventario final y DOS del mes de cierre
        logger.info("Paso 1: Calculando inventario final y DOS del mes de cierre...")
        dos_count = _compute_closing_month_inventory(session, mes_cierre_date_id)
        summary["pasos"]["1_inventario_cierre"] = {
            "dos_registros": dos_count,
        }

        # Paso 2: Cierre mensual (rolling 12)
        logger.info("Paso 2: Ejecutando cierre mensual...")
        close_result = run_monthly_close(session, mes_cierre_date_id)
        summary["pasos"]["2_cierre_mensual"] = close_result

        # Paso 3: Reconstruir DOS historicos
        logger.info("Paso 3: Reconstruyendo DOS historicos...")
        dos_hist_count = build_historical_dos(session, mes_cierre_date_id)
        summary["pasos"]["3_dos_historicos"] = {
            "registros": dos_hist_count,
        }

        # Paso 4: Determinar elegibilidad
        logger.info("Paso 4: Determinando cliente-SKU elegibles...")
        eligibility = get_eligible_client_skus(session, mes_cierre_date_id)
        summary["pasos"]["4_elegibilidad"] = {
            "elegibles": len(eligibility.eligible),
            "graduating": len(eligibility.graduating),
            "obsoletos": eligibility.obsolete_count,
            "excluidos": eligibility.excluded_count,
        }

        # Paso 4.5: Detectar clientes/SKUs nuevos en forecast
        logger.info("Paso 4.5: Detectando clientes/SKUs nuevos en forecast...")
        new_detection = detect_and_report_new_client_skus(session, mes_cierre_date_id)
        summary["pasos"]["4_5_nuevos_forecast"] = {
            "clientes_nuevos": len(new_detection["new_clients"]),
            "skus_nuevos": len(new_detection["new_skus"]),
            "skus_baja": len(new_detection["baja_skus"]),
            "total_reportados": new_detection["count"],
        }

        if not eligibility.eligible:
            logger.warning("No hay cliente-SKU elegibles para proyeccion estadistica.")
            # Aun podemos tener clientes nuevos registrados
            new_proj_count = 0
            if eligibility.graduating:
                logger.info("Paso 7.5: Proyectando %d clientes graduating desde forecast...",
                           len(eligibility.graduating))
                new_proj_count = project_new_client_skus(session, mes_cierre_date_id)
            summary["pasos"]["7_5_nuevos_proyectados"] = {"pares_proyectados": new_proj_count}
            summary["estado"] = "COMPLETADO"
            summary["mensaje"] = (
                f"Sin cliente-SKU elegibles para proyeccion estadistica. "
                f"{new_proj_count} pares nuevos proyectados desde forecast."
            )
            summary["duracion_segundos"] = (datetime.now() - start_time).total_seconds()
            session.commit()
            return summary

        # Paso 5: Proyectar Sales Out Unconstrained
        logger.info("Paso 5: Proyectando Sales Out Unconstrained...")
        so_count = project_sales_out_unconstrained(
            session, eligibility.eligible, mes_cierre_date_id
        )
        summary["pasos"]["5_sales_out_unc"] = {
            "registros": so_count,
        }

        # Paso 7: Proyectar Sales In + Inventario (incluye paso 6 DOS objectives)
        logger.info("Paso 7: Proyectando Sales In, inventarios y DOS...")
        si_inv_result = project_sales_in_and_inventory_unc(
            session, eligibility.eligible, mes_cierre_date_id
        )
        summary["pasos"]["7_sales_in_inv_unc"] = si_inv_result

        # Paso 7.5: Proyectar clientes nuevos registrados
        logger.info("Paso 7.5: Proyectando clientes nuevos registrados...")
        new_proj_count = project_new_client_skus(session, mes_cierre_date_id)
        summary["pasos"]["7_5_nuevos_proyectados"] = {
            "pares_proyectados": new_proj_count,
        }

        summary["estado"] = "COMPLETADO"
        summary["mensaje"] = (
            f"Proceso mensual completado. "
            f"{len(eligibility.eligible)} cliente-SKU proyectados (estadistico), "
            f"{len(eligibility.graduating)} graduating (forecast), "
            f"{new_proj_count} nuevos proyectados, "
            f"{eligibility.obsolete_count} obsoletos reportados."
        )
        summary["duracion_segundos"] = (datetime.now() - start_time).total_seconds()

        session.commit()
        logger.info("run_monthly_process completado para mes %d", mes_cierre_date_id)
        return summary

    except Exception as e:
        session.rollback()
        summary["estado"] = "ERROR"
        summary["mensaje"] = f"Error en proceso mensual: {str(e)}"
        summary["duracion_segundos"] = (datetime.now() - start_time).total_seconds()
        logger.exception("Error en run_monthly_process para mes %d", mes_cierre_date_id)
        raise
