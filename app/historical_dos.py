"""
historical_dos.py — Calculo de Days of Sale (DOS) historicos.

Para cada cliente-SKU y cada mes historico:
    - Toma inventario_final_unidades de fact_stock_cliente.
    - Calcula promedio de ventas Sales Out de los 12 meses anteriores a ese mes.
    - months_of_sale_historico = inventario_final / promedio_ventas_12m
    - days_of_sale_historico = months_of_sale_historico * 30

Si promedio_ventas_12m = 0, deja months/days como NULL.

Resultados se insertan en fact_dias_inventario_historico.
"""

import logging

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from .eligibility import _generate_date_id_range
from .models import (
    FactDiasInventarioHistorico,
    FactSalesOut,
    FactStockCliente,
)
from .monthly_close import _compute_month_minus_n

logger = logging.getLogger(__name__)


def build_historical_dos(session: Session, mes_cierre_date_id: int) -> int:
    """
    Construye los registros de DOS historico para los 12 meses de la ventana.

    Para cada combinacion cliente-SKU presente en fact_stock_cliente
    dentro de la ventana historica, calcula el promedio de ventas
    Sales Out de los 12 meses anteriores y deriva months/days of sale.

    Args:
        session: Sesion activa.
        mes_cierre_date_id: Ultimo mes historico (YYYYMM).

    Returns:
        Numero de registros insertados en fact_dias_inventario_historico.
    """
    # Limpiar registros previos para la ventana actual
    date_id_inicio = _compute_month_minus_n(mes_cierre_date_id, 11)
    historico_ids = _generate_date_id_range(date_id_inicio, mes_cierre_date_id)

    session.query(FactDiasInventarioHistorico).filter(
        FactDiasInventarioHistorico.date_id.in_(historico_ids)
    ).delete(synchronize_session="fetch")

    # Obtener todas las combinaciones cliente-SKU-mes con inventario
    stock_records = (
        session.query(
            FactStockCliente.date_id,
            FactStockCliente.cliente_id,
            FactStockCliente.sku_id,
            FactStockCliente.inventario_final_unidades,
        )
        .filter(FactStockCliente.date_id.in_(historico_ids))
        .all()
    )

    # Pre-cargar ventas Sales Out agrupadas por (date_id, cliente_id, sku_id)
    # Necesitamos hasta 24 meses atras desde el mes mas antiguo de la ventana
    date_id_mas_antiguo_ventas = _compute_month_minus_n(date_id_inicio, 12)
    sales_range = _generate_date_id_range(date_id_mas_antiguo_ventas, mes_cierre_date_id)

    sales_data = (
        session.query(
            FactSalesOut.date_id,
            FactSalesOut.cliente_id,
            FactSalesOut.sku_id,
            func.sum(FactSalesOut.unidades_sales_out).label("total"),
        )
        .filter(FactSalesOut.date_id.in_(sales_range))
        .group_by(
            FactSalesOut.date_id,
            FactSalesOut.cliente_id,
            FactSalesOut.sku_id,
        )
        .all()
    )

    # Construir lookup: (date_id, cliente_id, sku_id) -> total_ventas
    sales_lookup: dict[tuple[int, int, int], float] = {}
    for row in sales_data:
        sales_lookup[(row.date_id, row.cliente_id, row.sku_id)] = float(row.total or 0)

    inserted = 0

    for rec in stock_records:
        current_date_id = rec.date_id
        cliente_id = rec.cliente_id
        sku_id = rec.sku_id
        inv_final = float(rec.inventario_final_unidades or 0)

        # Calcular promedio de ventas de los 12 meses anteriores al mes actual
        prev_12_start = _compute_month_minus_n(current_date_id, 12)
        prev_12_end = _compute_month_minus_n(current_date_id, 1)
        prev_12_ids = _generate_date_id_range(prev_12_start, prev_12_end)

        total_ventas = sum(
            sales_lookup.get((did, cliente_id, sku_id), 0.0)
            for did in prev_12_ids
        )
        meses_con_datos = len(prev_12_ids)
        promedio_ventas = total_ventas / meses_con_datos if meses_con_datos > 0 else 0.0

        if promedio_ventas > 0:
            mos = inv_final / promedio_ventas
            dos = mos * 30
        else:
            mos = None
            dos = None

        registro = FactDiasInventarioHistorico(
            date_id=current_date_id,
            cliente_id=cliente_id,
            sku_id=sku_id,
            inventario_final=inv_final,
            promedio_ventas_12m=promedio_ventas if promedio_ventas > 0 else None,
            months_of_sale_historico=mos,
            days_of_sale_historico=dos,
        )
        session.add(registro)
        inserted += 1

    session.flush()
    logger.info(
        "build_historical_dos: %d registros insertados para ventana %d-%d",
        inserted,
        date_id_inicio,
        mes_cierre_date_id,
    )
    return inserted
