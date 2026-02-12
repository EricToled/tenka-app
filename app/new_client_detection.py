"""
new_client_detection.py — Deteccion de clientes/SKUs nuevos en forecast y proyeccion basada en forecast.

Reemplaza la logica anterior de _detect_forecast_without_catalog en monthly_process.py.

Funcionalidad:
    1. detect_and_report_new_client_skus:
       - Identifica pares cliente-SKU del forecast que no tienen datos historicos.
       - Clasifica: CLIENTE_NUEVO, SKU_NUEVO, CLIENTE_Y_SKU_NUEVO, SKU_BAJA.
       - Escribe reporte en rpt_forecast_sin_catalogo.

    2. project_new_client_skus:
       - Para pares registrados (registrado=TRUE), proyecta Sales In desde forecast.
       - Sales Out = 0 (asumido), inventario se acumula.
       - Inserta en fact_sales_in_unconstrained, fact_inventory_unconstrained, fact_new_client_projection.
"""

import logging
from datetime import datetime

from sqlalchemy import distinct, func
from sqlalchemy.orm import Session

from .eligibility import _generate_date_id_range
from .models import (
    DimCliente,
    DimSku,
    FactForecastSales,
    FactInventoryUnconstrained,
    FactNewClientProjection,
    FactSalesInUnconstrained,
    FactSalesOut,
    FactStockCliente,
    RptForecastSinCatalogo,
)
from .monthly_close import _compute_month_minus_n

logger = logging.getLogger(__name__)


def detect_and_report_new_client_skus(
    session: Session,
    mes_cierre_date_id: int,
) -> dict:
    """
    Detecta pares cliente-SKU del forecast sin datos historicos.

    Para cada par (cliente_id, sku_id) en fact_forecast_sales, verifica:
    - Si el cliente no tiene datos en fact_stock_cliente -> CLIENTE_NUEVO
    - Si el SKU tiene status 'Baja' -> SKU_BAJA
    - Si el par cliente-SKU no tiene datos historicos pero el cliente si -> SKU_NUEVO

    Preserva registros anteriores que ya tienen registrado=TRUE.

    Returns:
        dict con listas de nuevos clientes, SKUs y conteo total.
    """
    # Ventana historica
    date_id_inicio = _compute_month_minus_n(mes_cierre_date_id, 11)
    historico_ids = _generate_date_id_range(date_id_inicio, mes_cierre_date_id)

    # Preservar registros ya registrados
    already_registered = {
        (r.cliente_id, r.sku_id)
        for r in session.query(
            RptForecastSinCatalogo.cliente_id,
            RptForecastSinCatalogo.sku_id,
        )
        .filter(RptForecastSinCatalogo.registrado == True)
        .all()
        if r.cliente_id is not None and r.sku_id is not None
    }

    # Limpiar reportes no-registrados anteriores
    session.query(RptForecastSinCatalogo).filter(
        RptForecastSinCatalogo.registrado == False
    ).delete(synchronize_session="fetch")

    # Obtener combinaciones unicas de forecast
    forecast_pairs = (
        session.query(
            FactForecastSales.cliente_id,
            FactForecastSales.sku_id,
        )
        .distinct()
        .all()
    )

    # Clientes con datos historicos (en fact_stock_cliente)
    clients_with_history = {
        r[0]
        for r in session.query(distinct(FactStockCliente.cliente_id))
        .filter(FactStockCliente.date_id.in_(historico_ids))
        .all()
    }

    # Pares cliente-SKU con datos historicos (stock o ventas)
    pairs_with_stock = set(
        session.query(FactStockCliente.cliente_id, FactStockCliente.sku_id)
        .filter(FactStockCliente.date_id.in_(historico_ids))
        .distinct()
        .all()
    )
    pairs_with_sales = set(
        session.query(FactSalesOut.cliente_id, FactSalesOut.sku_id)
        .filter(FactSalesOut.date_id.in_(historico_ids))
        .distinct()
        .all()
    )
    pairs_with_history = pairs_with_stock | pairs_with_sales

    # SKUs dados de baja
    baja_skus = {
        r.sku_id
        for r in session.query(DimSku.sku_id).filter(DimSku.status == "Baja").all()
    }

    result = {
        "new_clients": [],
        "new_skus": [],
        "baja_skus": [],
        "count": 0,
    }

    reported = 0
    for f in forecast_pairs:
        cliente_id, sku_id = f.cliente_id, f.sku_id

        # Si ya esta registrado, no re-reportar
        if (cliente_id, sku_id) in already_registered:
            continue

        cliente = session.get(DimCliente, cliente_id)
        sku = session.get(DimSku, sku_id)

        # Determinar tipo de problema
        tipo_problema = None

        if sku_id in baja_skus:
            tipo_problema = "SKU_BAJA"
            result["baja_skus"].append({
                "cliente_id": cliente_id,
                "sku_id": sku_id,
                "cliente_nombre": cliente.cliente_nombre if cliente else "DESCONOCIDO",
                "upc": sku.upc if sku else None,
            })
        elif cliente_id not in clients_with_history:
            # Cliente completamente nuevo (sin datos historicos)
            if (cliente_id, sku_id) not in pairs_with_history:
                tipo_problema = "CLIENTE_NUEVO"
                result["new_clients"].append({
                    "cliente_id": cliente_id,
                    "sku_id": sku_id,
                    "cliente_nombre": cliente.cliente_nombre if cliente else "DESCONOCIDO",
                    "upc": sku.upc if sku else None,
                })
        elif (cliente_id, sku_id) not in pairs_with_history:
            # Cliente existe pero este SKU es nuevo para el
            tipo_problema = "SKU_NUEVO"
            result["new_skus"].append({
                "cliente_id": cliente_id,
                "sku_id": sku_id,
                "cliente_nombre": cliente.cliente_nombre if cliente else "DESCONOCIDO",
                "upc": sku.upc if sku else None,
            })

        if tipo_problema:
            comentario_map = {
                "CLIENTE_NUEVO": "Cliente nuevo sin datos historicos. Dar de alta para proyeccion basada en forecast.",
                "SKU_NUEVO": "SKU nuevo para este cliente. Dar de alta para proyeccion basada en forecast.",
                "SKU_BAJA": "SKU dado de baja. No se proyecta.",
            }
            registro = RptForecastSinCatalogo(
                cliente_nombre=cliente.cliente_nombre if cliente else "DESCONOCIDO",
                upc=sku.upc if sku else None,
                descripcion=sku.sku_descripcion if sku else "DESCONOCIDO",
                comentario=comentario_map.get(tipo_problema, ""),
                cliente_id=cliente_id,
                sku_id=sku_id,
                tipo_problema=tipo_problema,
                registrado=False,
            )
            session.add(registro)
            reported += 1

    session.flush()
    result["count"] = reported

    logger.info(
        "detect_and_report_new_client_skus: %d reportados "
        "(%d clientes nuevos, %d SKUs nuevos, %d SKUs baja)",
        reported,
        len(result["new_clients"]),
        len(result["new_skus"]),
        len(result["baja_skus"]),
    )
    return result


def project_new_client_skus(
    session: Session,
    mes_cierre_date_id: int,
) -> int:
    """
    Proyecta Sales In e inventario para pares cliente-SKU registrados como nuevos.

    Solo procesa pares con registrado=TRUE en rpt_forecast_sin_catalogo
    y tipo_problema in ('CLIENTE_NUEVO', 'SKU_NUEVO').

    Logica:
        - Sales In = unidades_forecast del forecast anual (directo, sin estadistica)
        - Sales Out = 0 (asumido)
        - Inventario Final(t) = Inventario Final(t-1) + Sales In(t)
        - Inventario Final(0) = Sales In(0)

    Inserta en:
        - fact_sales_in_unconstrained
        - fact_inventory_unconstrained (con DOS = NULL)
        - fact_new_client_projection (auditoria)

    Returns:
        Numero de pares cliente-SKU proyectados.
    """
    # Obtener pares registrados que son nuevos
    registered_pairs = (
        session.query(
            RptForecastSinCatalogo.cliente_id,
            RptForecastSinCatalogo.sku_id,
        )
        .filter(
            RptForecastSinCatalogo.registrado == True,
            RptForecastSinCatalogo.tipo_problema.in_(["CLIENTE_NUEVO", "SKU_NUEVO"]),
        )
        .distinct()
        .all()
    )

    if not registered_pairs:
        logger.info("project_new_client_skus: No hay pares registrados para proyectar.")
        return 0

    # Ventana de proyeccion: 12 meses despues del cierre
    mes_m = mes_cierre_date_id % 100
    mes_a = mes_cierre_date_id // 100
    proy_start = (mes_a * 100 + mes_m + 1) if mes_m < 12 else ((mes_a + 1) * 100 + 1)
    projection_ids = _generate_date_id_range(
        proy_start,
        _compute_month_plus_n(proy_start, 11),
    )

    # Limpiar proyecciones anteriores de estos pares
    pair_tuples = [(r.cliente_id, r.sku_id) for r in registered_pairs]
    for cid, sid in pair_tuples:
        session.query(FactNewClientProjection).filter(
            FactNewClientProjection.cliente_id == cid,
            FactNewClientProjection.sku_id == sid,
        ).delete(synchronize_session="fetch")
        session.query(FactSalesInUnconstrained).filter(
            FactSalesInUnconstrained.cliente_id == cid,
            FactSalesInUnconstrained.sku_id == sid,
            FactSalesInUnconstrained.date_id.in_(projection_ids),
        ).delete(synchronize_session="fetch")
        session.query(FactInventoryUnconstrained).filter(
            FactInventoryUnconstrained.cliente_id == cid,
            FactInventoryUnconstrained.sku_id == sid,
            FactInventoryUnconstrained.date_id.in_(projection_ids),
        ).delete(synchronize_session="fetch")

    projected_count = 0

    for cid, sid in pair_tuples:
        # Obtener forecast por mes para este par
        forecast_by_month = {}
        forecasts = (
            session.query(
                FactForecastSales.date_id,
                func.sum(FactForecastSales.unidades_forecast).label("total"),
            )
            .filter(
                FactForecastSales.cliente_id == cid,
                FactForecastSales.sku_id == sid,
                FactForecastSales.date_id.in_(projection_ids),
            )
            .group_by(FactForecastSales.date_id)
            .all()
        )
        for f in forecasts:
            forecast_by_month[f.date_id] = float(f.total or 0)

        if not forecast_by_month:
            continue

        # Proyectar: Sales In = forecast, inventario acumulado
        inv_acumulado = 0.0
        for date_id in projection_ids:
            sales_in = forecast_by_month.get(date_id, 0.0)
            inv_acumulado += sales_in

            # fact_sales_in_unconstrained
            session.add(FactSalesInUnconstrained(
                date_id=date_id,
                cliente_id=cid,
                sku_id=sid,
                unidades_sales_in_unc=float(round(sales_in, 4)),
            ))

            # fact_inventory_unconstrained (DOS = NULL porque Sales Out = 0)
            session.add(FactInventoryUnconstrained(
                date_id=date_id,
                cliente_id=cid,
                sku_id=sid,
                inventario_final_unc=float(round(inv_acumulado, 4)),
                months_of_sale_unc=None,
                days_of_sale_unc=None,
            ))

            # fact_new_client_projection (auditoria)
            session.add(FactNewClientProjection(
                date_id=date_id,
                cliente_id=cid,
                sku_id=sid,
                unidades_sales_in=float(round(sales_in, 4)),
                inventario_final_acumulado=float(round(inv_acumulado, 4)),
                origen="FORECAST",
            ))

        projected_count += 1

    session.flush()
    logger.info(
        "project_new_client_skus: %d pares cliente-SKU proyectados desde forecast.",
        projected_count,
    )
    return projected_count


def _compute_month_plus_n(date_id: int, n: int) -> int:
    """Avanza n meses desde date_id (YYYYMM)."""
    anio = date_id // 100
    mes = date_id % 100
    total_months = (anio * 12 + mes - 1) + n
    new_anio = total_months // 12
    new_mes = total_months % 12 + 1
    return new_anio * 100 + new_mes
