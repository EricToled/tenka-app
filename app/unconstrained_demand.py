"""
unconstrained_demand.py — Proyeccion de Unconstrained Demand.

Implementa:
    Seccion 6: Proyeccion de Sales Out Unconstrained (modelo estadistico + forecast fallback)
    Seccion 7: Objetivos de Days of Sale (DOS) por mes proyectado
    Seccion 8: Calculo de Sales In Unconstrained e inventarios
    Seccion 9: Reajuste iterativo por inventarios altos (evitar Sales In negativas)

Invariantes:
    - Ningun valor de unidades_sales_out_unc menor al minimo historico (>0)
    - Ningun valor de unidades_sales_in_unc negativo
    - Inventarios finales y DOS consistentes con las restricciones
"""

import logging
import math
from dataclasses import dataclass

import numpy as np
from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from .eligibility import EligibleClientSku, _generate_date_id_range
from .models import (
    FactDiasInventarioHistorico,
    FactForecastSales,
    FactInventoryUnconstrained,
    FactSalesInUnconstrained,
    FactSalesOut,
    FactSalesOutUnconstrained,
    FactStockCliente,
)
from .monthly_close import _compute_month_minus_n

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# SECCION 6: PROYECCION DE SALES OUT UNCONSTRAINED
# ─────────────────────────────────────────────


def _get_historical_sales_out_series(
    session: Session,
    cliente_id: int,
    sku_id: int,
    date_ids: list[int],
) -> list[float]:
    """
    Obtiene la serie historica de Sales Out para un cliente-SKU,
    ordenada por date_id. Retorna 0 para meses sin datos.
    """
    sales = (
        session.query(
            FactSalesOut.date_id,
            func.sum(FactSalesOut.unidades_sales_out).label("total"),
        )
        .filter(
            FactSalesOut.cliente_id == cliente_id,
            FactSalesOut.sku_id == sku_id,
            FactSalesOut.date_id.in_(date_ids),
        )
        .group_by(FactSalesOut.date_id)
        .all()
    )
    lookup = {r.date_id: float(r.total or 0) for r in sales}
    return [lookup.get(did, 0.0) for did in date_ids]


def _get_forecast_series(
    session: Session,
    cliente_id: int,
    sku_id: int,
    projection_date_ids: list[int],
) -> list[float]:
    """
    Obtiene valores de forecast comercial para los 12 meses proyectados.
    Retorna 0 para meses sin forecast.
    """
    forecasts = (
        session.query(
            FactForecastSales.date_id,
            func.sum(FactForecastSales.unidades_forecast).label("total"),
        )
        .filter(
            FactForecastSales.cliente_id == cliente_id,
            FactForecastSales.sku_id == sku_id,
            FactForecastSales.date_id.in_(projection_date_ids),
        )
        .group_by(FactForecastSales.date_id)
        .all()
    )
    lookup = {r.date_id: float(r.total or 0) for r in forecasts}
    return [lookup.get(did, 0.0) for did in projection_date_ids]


def _linear_trend_projection(
    series: list[float],
    n_future: int = 12,
    use_trend: bool = True,
) -> list[float]:
    """
    Modelo estadistico: regresion lineal sobre la serie temporal.
    Proyecta n_future valores futuros.

    Reglas:
        - use_trend=True: regresion lineal con pendiente (tendencia).
          Solo para SKUs con >= 8 meses con ventas Y cuyo inicio de
          operaciones es anterior al inicio de la ventana historica
          (operacion madura con 12 meses completos).
        - use_trend=False: usa el PROMEDIO de los valores historicos > 0
          (proyeccion plana, sin tendencia creciente). Se aplica a SKUs
          con menos de 8 meses de ventas o con inicio de operacion
          dentro de la ventana historica (clientes recientes como Walmart).

    Args:
        series: Serie historica de ventas (ordenada cronologicamente).
        n_future: Numero de valores futuros a proyectar.
        use_trend: Si True, aplica pendiente de regresion. Si False,
                   proyecta con promedio (sin tendencia).

    Returns:
        Lista de n_future valores proyectados.
    """
    n = len(series)
    if n == 0:
        return [0.0] * n_future

    # Filtrar puntos no nulos para la regresion
    x_vals = []
    y_vals = []
    for i, v in enumerate(series):
        if v > 0:
            x_vals.append(float(i))
            y_vals.append(v)

    if not y_vals:
        return [0.0] * n_future

    if len(x_vals) < 2 or not use_trend:
        # Sin tendencia: usar promedio de valores > 0
        avg = float(np.mean(y_vals))
        return [max(0.0, avg)] * n_future

    x = np.array(x_vals)
    y = np.array(y_vals)

    # Regresion lineal: y = slope * x + intercept
    coeffs = np.polyfit(x, y, 1)
    slope = float(coeffs[0])
    intercept = float(coeffs[1])

    projected = []
    for j in range(n_future):
        t = n + j
        val = slope * t + intercept
        projected.append(max(0.0, val))

    return projected


def project_sales_out_unconstrained(
    session: Session,
    eligible: list[EligibleClientSku],
    mes_cierre_date_id: int,
) -> int:
    """
    Proyecta Sales Out Unconstrained para todos los cliente-SKU elegibles.

    Seccion 6 completa:
        6.1 Serie de entrada (historica o forecast)
        6.2 Modelo estadistico (regresion lineal)
        6.3 Fallback a forecast cuando < 3 meses efectivos
        6.4 Piso minimo historico de Sales Out

    Args:
        session: Sesion activa.
        eligible: Lista de EligibleClientSku del modulo de elegibilidad.
        mes_cierre_date_id: Ultimo mes historico.

    Returns:
        Numero de registros insertados en fact_sales_out_unconstrained.
    """
    # Calcular date_ids de proyeccion (12 meses despues del cierre)
    mes_cierre_mes = mes_cierre_date_id % 100
    mes_cierre_anio = mes_cierre_date_id // 100
    if mes_cierre_mes == 12:
        proy_start = (mes_cierre_anio + 1) * 100 + 1
    else:
        proy_start = mes_cierre_anio * 100 + mes_cierre_mes + 1

    # Generar 12 meses de proyeccion
    projection_ids = []
    current = proy_start
    for _ in range(12):
        projection_ids.append(current)
        m = current % 100
        a = current // 100
        current = (a * 100 + m + 1) if m < 12 else ((a + 1) * 100 + 1)

    # Ventana historica completa
    date_id_inicio = _compute_month_minus_n(mes_cierre_date_id, 11)
    historico_ids = _generate_date_id_range(date_id_inicio, mes_cierre_date_id)

    # Limpiar proyecciones anteriores
    session.query(FactSalesOutUnconstrained).filter(
        FactSalesOutUnconstrained.date_id.in_(projection_ids)
    ).delete(synchronize_session="fetch")

    inserted = 0

    for item in eligible:
        # 6.1 Construir serie de entrada
        if item.use_forecast_as_base:
            # < 3 meses efectivos: usar forecast directamente
            projected = _get_forecast_series(
                session, item.cliente_id, item.sku_id, projection_ids
            )
        else:
            # >= 3 meses: usar serie historica desde inicio de operaciones
            effective_ids = _generate_date_id_range(
                item.date_id_inicio_operaciones, mes_cierre_date_id
            )
            series = _get_historical_sales_out_series(
                session, item.cliente_id, item.sku_id, effective_ids
            )
            # 6.2 Modelo estadistico
            # use_trend=True solo si >= 8 meses con ventas Y operacion
            # anterior al inicio de ventana historica
            projected = _linear_trend_projection(
                series, 12, use_trend=item.use_trend
            )

        # 6.4 Piso minimo historico
        historical_series = _get_historical_sales_out_series(
            session, item.cliente_id, item.sku_id, historico_ids
        )
        positive_values = [v for v in historical_series if v > 0]
        min_historico = min(positive_values) if positive_values else 0.0

        # Aplicar piso
        for i in range(12):
            if projected[i] < min_historico and min_historico > 0:
                projected[i] = min_historico

        # Insertar registros (convertir np.float64 → float nativo para psycopg2)
        for i, date_id in enumerate(projection_ids):
            registro = FactSalesOutUnconstrained(
                date_id=date_id,
                cliente_id=item.cliente_id,
                sku_id=item.sku_id,
                unidades_sales_out_unc=float(round(projected[i], 4)),
            )
            session.add(registro)
            inserted += 1

    session.flush()
    logger.info(
        "project_sales_out_unconstrained: %d registros insertados",
        inserted,
    )
    return inserted


# ─────────────────────────────────────────────
# SECCION 7: OBJETIVOS DE DOS POR MES PROYECTADO
# ─────────────────────────────────────────────


def compute_dos_objectives(dos_0: float) -> list[float]:
    """
    Calcula los 12 objetivos de Days of Sale para los meses proyectados.

    Reglas:
        7.1 DOS_0 > 90:
            - Meses 1-6: reduccion lineal de DOS_0 a 90
            - Meses 7-12: reduccion lineal de 90 a 60

        7.2 60 <= DOS_0 <= 90:
            - Meses 1-12: reduccion lineal de DOS_0 a 60

        7.3 DOS_0 < 60:
            - Todos los meses: objetivo = 60

    Args:
        dos_0: Days of Sale del ultimo mes historico.

    Returns:
        Lista de 12 valores de DOS objetivo (uno por mes proyectado).
    """
    objectives = [0.0] * 12

    if dos_0 is None or dos_0 <= 0:
        # Sin datos de DOS: objetivo conservador de 60 dias
        return [60.0] * 12

    if dos_0 > 90:
        # Tramo 1: meses 1-6, de DOS_0 a 90
        exceso = dos_0 - 90
        decremento_1 = exceso / 6
        for i in range(6):
            objectives[i] = dos_0 - (i + 1) * decremento_1

        # Tramo 2: meses 7-12, de 90 a 60
        decremento_2 = 30 / 6  # = 5
        for i in range(6, 12):
            objectives[i] = 90 - (i - 6 + 1) * decremento_2

    elif dos_0 >= 60:
        # Reduccion lineal de DOS_0 a 60 en 12 meses
        diferencia = dos_0 - 60
        decremento = diferencia / 12
        for i in range(12):
            objectives[i] = dos_0 - (i + 1) * decremento

    else:
        # DOS_0 < 60: mantener en 60 (no bajar mas)
        objectives = [60.0] * 12

    return objectives


# ─────────────────────────────────────────────
# SECCIONES 8-9: SALES IN, INVENTARIO Y REAJUSTE ITERATIVO
# ─────────────────────────────────────────────


def project_sales_in_and_inventory_unc(
    session: Session,
    eligible: list[EligibleClientSku],
    mes_cierre_date_id: int,
) -> dict:
    """
    Proyecta Sales In Unconstrained, inventario final y DOS para los 12
    meses proyectados, con reajuste iterativo para evitar Sales In negativas.

    Seccion 8:
        Para cada mes t (1..12):
        - inv_inicial_t = inv_final_(t-1)  [t=1: ultimo mes historico]
        - sales_out_t = de fact_sales_out_unconstrained
        - promedio_ventas_12m = avg de ultimos 12 meses (historicos + proyectados)
        - DOS_objetivo_t = de compute_dos_objectives
        - sales_in_t = inv_final_deseado + sales_out_t - inv_inicial_t
          donde inv_final_deseado = DOS_objetivo_t / 30 * promedio_ventas_12m
        - Si sales_in_t < 0 → sales_in_t = 0, recalcular inv_final

    Seccion 9:
        Algoritmo iterativo: repite t=1..12 hasta que todas las Sales In >= 0.

    Args:
        session: Sesion activa.
        eligible: Lista de EligibleClientSku.
        mes_cierre_date_id: Ultimo mes historico.

    Returns:
        Diccionario con conteos de registros insertados.
    """
    # Calcular date_ids de proyeccion
    mes_m = mes_cierre_date_id % 100
    mes_a = mes_cierre_date_id // 100
    proy_start = (mes_a * 100 + mes_m + 1) if mes_m < 12 else ((mes_a + 1) * 100 + 1)

    projection_ids = []
    current = proy_start
    for _ in range(12):
        projection_ids.append(current)
        m = current % 100
        a = current // 100
        current = (a * 100 + m + 1) if m < 12 else ((a + 1) * 100 + 1)

    # Ventana historica
    date_id_inicio = _compute_month_minus_n(mes_cierre_date_id, 11)
    historico_ids = _generate_date_id_range(date_id_inicio, mes_cierre_date_id)

    # Limpiar proyecciones anteriores
    session.query(FactSalesInUnconstrained).filter(
        FactSalesInUnconstrained.date_id.in_(projection_ids)
    ).delete(synchronize_session="fetch")
    session.query(FactInventoryUnconstrained).filter(
        FactInventoryUnconstrained.date_id.in_(projection_ids)
    ).delete(synchronize_session="fetch")

    # Pre-cargar ventas historicas Sales Out agrupadas
    hist_sales = (
        session.query(
            FactSalesOut.date_id,
            FactSalesOut.cliente_id,
            FactSalesOut.sku_id,
            func.sum(FactSalesOut.unidades_sales_out).label("total"),
        )
        .filter(FactSalesOut.date_id.in_(historico_ids))
        .group_by(FactSalesOut.date_id, FactSalesOut.cliente_id, FactSalesOut.sku_id)
        .all()
    )
    hist_sales_lookup: dict[tuple[int, int, int], float] = {}
    for r in hist_sales:
        hist_sales_lookup[(r.date_id, r.cliente_id, r.sku_id)] = float(r.total or 0)

    # Pre-cargar Sales Out Unconstrained proyectadas
    proj_sales_out = (
        session.query(
            FactSalesOutUnconstrained.date_id,
            FactSalesOutUnconstrained.cliente_id,
            FactSalesOutUnconstrained.sku_id,
            FactSalesOutUnconstrained.unidades_sales_out_unc,
        )
        .filter(FactSalesOutUnconstrained.date_id.in_(projection_ids))
        .all()
    )
    proj_sales_out_lookup: dict[tuple[int, int, int], float] = {}
    for r in proj_sales_out:
        proj_sales_out_lookup[(r.date_id, r.cliente_id, r.sku_id)] = float(
            r.unidades_sales_out_unc or 0
        )

    # Pre-cargar DOS del ultimo mes historico
    dos_last = (
        session.query(
            FactDiasInventarioHistorico.cliente_id,
            FactDiasInventarioHistorico.sku_id,
            FactDiasInventarioHistorico.days_of_sale_historico,
        )
        .filter(FactDiasInventarioHistorico.date_id == mes_cierre_date_id)
        .all()
    )
    dos_last_lookup: dict[tuple[int, int], float | None] = {}
    for r in dos_last:
        val = float(r.days_of_sale_historico) if r.days_of_sale_historico is not None else None
        dos_last_lookup[(r.cliente_id, r.sku_id)] = val

    # Pre-cargar inventario final del ultimo mes historico
    inv_last = (
        session.query(
            FactStockCliente.cliente_id,
            FactStockCliente.sku_id,
            FactStockCliente.inventario_final_unidades,
        )
        .filter(FactStockCliente.date_id == mes_cierre_date_id)
        .all()
    )
    inv_last_lookup: dict[tuple[int, int], float] = {}
    for r in inv_last:
        inv_last_lookup[(r.cliente_id, r.sku_id)] = float(r.inventario_final_unidades or 0)

    # Resultados a insertar
    results_sales_in: list[FactSalesInUnconstrained] = []
    results_inventory: list[FactInventoryUnconstrained] = []

    for item in eligible:
        cid = item.cliente_id
        sid = item.sku_id

        # DOS del ultimo mes historico
        dos_0 = dos_last_lookup.get((cid, sid))
        dos_objectives = compute_dos_objectives(dos_0)

        # Sales Out proyectadas para este cliente-SKU
        sales_out_proj = [
            proj_sales_out_lookup.get((did, cid, sid), 0.0)
            for did in projection_ids
        ]

        # Serie historica de Sales Out (para calcular promedios rolling)
        hist_series = [
            hist_sales_lookup.get((did, cid, sid), 0.0)
            for did in historico_ids
        ]

        # Inventario final del ultimo mes historico
        inv_inicial_0 = inv_last_lookup.get((cid, sid), 0.0)

        # Seccion 9: Algoritmo iterativo
        sales_in, inv_final, mos, dos = _iterative_projection(
            hist_series=hist_series,
            historico_ids=historico_ids,
            projection_ids=projection_ids,
            sales_out_proj=sales_out_proj,
            dos_objectives=dos_objectives,
            inv_inicial_0=inv_inicial_0,
        )

        # Crear registros (convertir a float nativo para psycopg2)
        for i, date_id in enumerate(projection_ids):
            results_sales_in.append(
                FactSalesInUnconstrained(
                    date_id=date_id,
                    cliente_id=cid,
                    sku_id=sid,
                    unidades_sales_in_unc=float(round(sales_in[i], 4)),
                )
            )
            results_inventory.append(
                FactInventoryUnconstrained(
                    date_id=date_id,
                    cliente_id=cid,
                    sku_id=sid,
                    inventario_final_unc=float(round(inv_final[i], 4)),
                    months_of_sale_unc=float(round(mos[i], 4)) if mos[i] is not None else None,
                    days_of_sale_unc=float(round(dos[i], 4)) if dos[i] is not None else None,
                )
            )

    session.add_all(results_sales_in)
    session.add_all(results_inventory)
    session.flush()

    logger.info(
        "project_sales_in_and_inventory_unc: %d sales_in, %d inventory insertados",
        len(results_sales_in),
        len(results_inventory),
    )
    return {
        "sales_in_count": len(results_sales_in),
        "inventory_count": len(results_inventory),
    }


def _iterative_projection(
    hist_series: list[float],
    historico_ids: list[int],
    projection_ids: list[int],
    sales_out_proj: list[float],
    dos_objectives: list[float],
    inv_inicial_0: float,
    max_iterations: int = 20,
) -> tuple[list[float], list[float], list[float | None], list[float | None]]:
    """
    Algoritmo iterativo de proyeccion (Secciones 8-9).

    Repite el calculo de t=1..12 hasta que todas las Sales In >= 0
    o se alcance max_iterations.

    Args:
        hist_series: Ventas historicas Sales Out (12 valores).
        historico_ids: date_ids historicos.
        projection_ids: date_ids de proyeccion.
        sales_out_proj: Sales Out Unconstrained proyectadas (12 valores).
        dos_objectives: Objetivos de DOS por mes (12 valores).
        inv_inicial_0: Inventario final del ultimo mes historico.
        max_iterations: Maximo de iteraciones del reajuste.

    Returns:
        Tupla de 4 listas de 12 valores:
            (sales_in, inv_final, months_of_sale, days_of_sale)
    """
    n_proj = 12

    # Inicializar Sales In como arrays
    sales_in = [0.0] * n_proj
    inv_final = [0.0] * n_proj
    mos = [None] * n_proj
    dos = [None] * n_proj

    # Serie combinada para promedios rolling:
    # [12 meses historicos] + [12 meses proyectados Sales Out]
    combined_sales = list(hist_series) + list(sales_out_proj)

    for iteration in range(max_iterations):
        any_negative = False

        for t in range(n_proj):
            # Inventario inicial del mes t
            if t == 0:
                inv_ini = inv_inicial_0
            else:
                inv_ini = inv_final[t - 1]

            # Sales Out del mes t
            so_t = sales_out_proj[t]

            # Promedio de ventas de los 12 meses anteriores al mes t
            # Los 12 meses anteriores al mes t de proyeccion:
            #   - Si t=0, los 12 meses historicos (indices 0..11 de combined)
            #   - Si t=1, historicos[1..11] + proyectados[0]
            #   - etc.
            start_idx = t  # en combined_sales, los 12 meses anteriores empiezan en indice t
            end_idx = t + 12
            prev_12 = combined_sales[start_idx:end_idx]
            avg_12 = sum(prev_12) / 12 if len(prev_12) == 12 else (
                sum(prev_12) / len(prev_12) if prev_12 else 0.0
            )

            # DOS objetivo del mes t
            dos_obj = dos_objectives[t]

            # Inventario final deseado para cumplir el DOS objetivo
            if avg_12 > 0:
                inv_final_deseado = (dos_obj / 30.0) * avg_12
            else:
                inv_final_deseado = 0.0

            # Sales In necesarias
            si_t = inv_final_deseado + so_t - inv_ini

            # Restriccion: Sales In >= 0
            if si_t < 0:
                si_t = 0.0
                any_negative = True  # Hubo ajuste, necesita otra iteracion

            # Recalcular inventario final real
            inv_f = inv_ini + si_t - so_t
            if inv_f < 0:
                inv_f = 0.0

            # Calcular DOS y MOS reales
            if avg_12 > 0:
                mos_t = inv_f / avg_12
                dos_t = mos_t * 30
            else:
                mos_t = None
                dos_t = None

            sales_in[t] = si_t
            inv_final[t] = inv_f
            mos[t] = mos_t
            dos[t] = dos_t

        if not any_negative:
            break

        # Actualizar combined_sales con los nuevos Sales Out proyectados
        # (los Sales Out no cambian, pero los inventarios si afectan)
        # Los Sales Out proyectados son fijos, solo recalculamos con los
        # nuevos inventarios como base

    return sales_in, inv_final, mos, dos
