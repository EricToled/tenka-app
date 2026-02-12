"""
constraint_demand.py — Fase 3: Constraint Demand.

Calcula, para cada SKU y horizonte de 12 meses de proyeccion:
    1. Inventario interno proyectado (simulacion inicial permitiendo negativos)
    2. Plan de ordenes de compra (POs) para cumplir politica de L meses de cobertura
    3. Inventario interno proyectado definitivo (sin negativos, con POs)
    4. Sales In Constrained (limitadas por stock interno en ventana 1..L)
    5. Lost Sales por OOS irrecuperable en ventana de lead time

Invariantes:
    - Todos los calculos de inventario interno son a nivel SKU global (no por cliente).
    - Fase 3 no recalcula ni toca Sales Out.
    - Lead time es por SKU (DimSku.lead_time_meses).
    - La politica de inventario TENKA: mantener L meses de cobertura.
"""

import json
import logging
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from .eligibility import _generate_date_id_range
from .models import (
    DimSku,
    FactInventarioInterno,
    FactInventarioTransito,
    FactInventoryInternalConstrained,
    FactPoInterno,
    FactSalesInConstrained,
    FactSalesInUnconstrained,
    RptLostSalesOos,
)
from .monthly_close import _compute_month_minus_n

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────


def _compute_month_plus_n(date_id: int, n: int) -> int:
    """Avanza n meses desde date_id (YYYYMM)."""
    anio = date_id // 100
    mes = date_id % 100
    total_months = (anio * 12 + mes - 1) + n
    new_anio = total_months // 12
    new_mes = total_months % 12 + 1
    return new_anio * 100 + new_mes


def _get_projection_ids(mes_cierre_date_id: int) -> list[int]:
    """Genera los 12 date_ids de proyeccion posteriores al mes de cierre."""
    proy_start = _compute_month_plus_n(mes_cierre_date_id, 1)
    proy_end = _compute_month_plus_n(mes_cierre_date_id, 12)
    return _generate_date_id_range(proy_start, proy_end)


def _get_skus_with_demand(session: Session, projection_ids: list[int]) -> list[int]:
    """Obtiene IDs de SKUs que tienen Sales In Unconstrained en la ventana de proyeccion."""
    rows = (
        session.query(FactSalesInUnconstrained.sku_id)
        .filter(FactSalesInUnconstrained.date_id.in_(projection_ids))
        .distinct()
        .all()
    )
    return sorted(r.sku_id for r in rows)


def _get_internal_inventory_initial(
    session: Session,
    sku_id: int,
    mes_cierre_date_id: int,
) -> float:
    """Inventario interno final del ultimo mes historico para un SKU."""
    result = (
        session.query(func.sum(FactInventarioInterno.stock_interno_disponible))
        .filter(
            FactInventarioInterno.sku_id == sku_id,
            FactInventarioInterno.date_id == mes_cierre_date_id,
        )
        .scalar()
    )
    return float(result or 0)


def _get_demand_by_month(
    session: Session,
    sku_id: int,
    projection_ids: list[int],
) -> dict[int, float]:
    """
    Demanda agregada (Sales In Unconstrained) por SKU y mes,
    sumando todos los clientes.
    """
    rows = (
        session.query(
            FactSalesInUnconstrained.date_id,
            func.sum(FactSalesInUnconstrained.unidades_sales_in_unc).label("total"),
        )
        .filter(
            FactSalesInUnconstrained.sku_id == sku_id,
            FactSalesInUnconstrained.date_id.in_(projection_ids),
        )
        .group_by(FactSalesInUnconstrained.date_id)
        .all()
    )
    return {r.date_id: float(r.total or 0) for r in rows}


def _get_transit_arrivals_by_month(
    session: Session,
    sku_id: int,
    projection_ids: list[int],
) -> dict[int, float]:
    """
    Llegadas de inventario en transito por SKU y mes.
    Preferencia: stock_en_transito_confirmado si existe, sino stock_en_transito.
    """
    rows = (
        session.query(
            FactInventarioTransito.date_id,
            FactInventarioTransito.stock_en_transito,
            FactInventarioTransito.stock_en_transito_confirmado,
        )
        .filter(
            FactInventarioTransito.sku_id == sku_id,
            FactInventarioTransito.date_id.in_(projection_ids),
        )
        .all()
    )
    arrivals: dict[int, float] = {}
    for r in rows:
        val = float(r.stock_en_transito_confirmado or r.stock_en_transito or 0)
        arrivals[r.date_id] = arrivals.get(r.date_id, 0) + val
    return arrivals


def _get_lead_time(session: Session, sku_id: int) -> int:
    """Obtiene el lead time en meses para un SKU. Default 4."""
    sku = session.get(DimSku, sku_id)
    if sku and sku.lead_time_meses:
        return sku.lead_time_meses
    return 4


# ─────────────────────────────────────────────
# PASO 1: SIMULACION DE INVENTARIO INTERNO (PERMITIENDO NEGATIVOS)
# ─────────────────────────────────────────────


def simulate_internal_inventory_window(
    session: Session,
    mes_cierre_date_id: int,
) -> int:
    """
    Calcula el inventario interno proyectado de 'prueba' para cada SKU
    en los meses 1..L de proyeccion, permitiendo inventario negativo.

    Usa:
    - Inventario interno final del ultimo mes historico (FactInventarioInterno).
    - Sales In Unconstrained agregadas por SKU (suma de todos los clientes).
    - Llegadas ya programadas en FactInventarioTransito.

    Inserta resultados iniciales en fact_inventory_internal_constrained.

    Returns:
        Numero de registros insertados.
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)
    sku_ids = _get_skus_with_demand(session, projection_ids)

    if not sku_ids:
        logger.warning("simulate_internal_inventory_window: No hay SKUs con demanda.")
        return 0

    # Limpiar datos previos de simulacion
    session.query(FactInventoryInternalConstrained).filter(
        FactInventoryInternalConstrained.date_id.in_(projection_ids),
    ).delete(synchronize_session="fetch")

    inserted = 0

    for sku_id in sku_ids:
        L = _get_lead_time(session, sku_id)
        inv_int_0 = _get_internal_inventory_initial(session, sku_id, mes_cierre_date_id)
        demand = _get_demand_by_month(session, sku_id, projection_ids)
        arrivals = _get_transit_arrivals_by_month(session, sku_id, projection_ids)

        # Solo simular meses 1..L (ventana de lead time)
        window_size = min(L, len(projection_ids))
        inv_prev = inv_int_0

        for t in range(window_size):
            date_id_t = projection_ids[t]
            demand_t = demand.get(date_id_t, 0.0)
            arrivals_t = arrivals.get(date_id_t, 0.0)

            inv_t = inv_prev - demand_t + arrivals_t
            # Se permite negativo en esta simulacion

            session.add(FactInventoryInternalConstrained(
                date_id=date_id_t,
                sku_id=sku_id,
                inventario_final_int=float(round(inv_t, 4)),
            ))
            inserted += 1
            inv_prev = inv_t

    session.flush()
    logger.info(
        "simulate_internal_inventory_window: %d registros insertados para %d SKUs",
        inserted, len(sku_ids),
    )
    return inserted


# ─────────────────────────────────────────────
# PASO 2: GENERACION DE POs (POLITICA L MESES DE INVENTARIO)
# ─────────────────────────────────────────────


def generate_po_plan(
    session: Session,
    mes_cierre_date_id: int,
) -> int:
    """
    Genera POs internas por SKU segun la politica de tener L meses de inventario.

    Para cada SKU y cada mes t en 1..L:
    - Calcula el inventario de prueba al final del mes t + (L-1).
    - Calcula la demanda de los L meses posteriores (t+L .. t+2L-1).
    - Si la capacidad (inv - demanda_futura) < 0, genera PO:
        date_id_orden = mes t
        date_id_llegada = mes t + L
        unidades_po = abs(capacidad)

    Inserta en fact_po_interno.

    Returns:
        Numero de POs insertadas.
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)
    sku_ids = _get_skus_with_demand(session, projection_ids)

    # Limpiar POs previas
    session.query(FactPoInterno).delete(synchronize_session="fetch")

    po_count = 0

    for sku_id in sku_ids:
        L = _get_lead_time(session, sku_id)
        inv_int_0 = _get_internal_inventory_initial(session, sku_id, mes_cierre_date_id)
        demand = _get_demand_by_month(session, sku_id, projection_ids)
        arrivals = _get_transit_arrivals_by_month(session, sku_id, projection_ids)

        n_proj = len(projection_ids)

        # Reconstruir inventario de prueba para todos los meses 1..12
        # (incluye simulacion completa antes de POs, para evaluar posicion)
        inv_trial = [0.0] * n_proj
        inv_prev = inv_int_0
        for t in range(n_proj):
            did = projection_ids[t]
            d = demand.get(did, 0.0)
            a = arrivals.get(did, 0.0)
            inv_trial[t] = inv_prev - d + a
            inv_prev = inv_trial[t]

        # Track PO arrivals generated in this loop to account for earlier POs
        po_arrivals = {}  # date_id -> units

        # Evaluar TODOS los meses del horizonte (no solo 1..L).
        # La politica de L meses de cobertura es continua:
        # en cada mes t se evalua si el inventario al final de t+(L-1)
        # cubre la demanda de los siguientes L meses. Si no, se genera PO.
        for t in range(n_proj):
            # El indice del ultimo mes de la ventana de lead time desde t
            # es t + (L - 1). Queremos evaluar inventario al final de ese mes.
            look_at_idx = t + L - 1
            if look_at_idx >= n_proj:
                break

            # Inventario de prueba en el mes look_at_idx,
            # incluyendo POs ya generadas en iteraciones anteriores
            inv_at_look = inv_trial[look_at_idx]
            for po_did, po_units in po_arrivals.items():
                # Encontrar indice de ese date_id
                if po_did in projection_ids:
                    po_idx = projection_ids.index(po_did)
                    if po_idx <= look_at_idx:
                        inv_at_look += po_units

            # Demanda de los proximos L meses DESPUES de look_at_idx
            future_demand = 0.0
            for j in range(look_at_idx + 1, min(look_at_idx + L + 1, n_proj)):
                future_demand += demand.get(projection_ids[j], 0.0)

            capacidad = inv_at_look - future_demand

            if capacidad < -0.5:  # umbral minimo para evitar POs de 0 por redondeo
                po_units = abs(capacidad)
                # La PO se genera en mes t, llega en mes t + L
                arrival_idx = t + L
                if arrival_idx < n_proj:
                    date_id_orden = projection_ids[t]
                    date_id_llegada = projection_ids[arrival_idx]

                    session.add(FactPoInterno(
                        sku_id=sku_id,
                        date_id_orden=date_id_orden,
                        date_id_llegada=date_id_llegada,
                        unidades_po=float(round(po_units, 4)),
                    ))
                    po_count += 1

                    # Track this PO's arrival for next iterations
                    po_arrivals[date_id_llegada] = (
                        po_arrivals.get(date_id_llegada, 0) + po_units
                    )

    session.flush()
    logger.info("generate_po_plan: %d POs generadas para %d SKUs", po_count, len(sku_ids))
    return po_count


# ─────────────────────────────────────────────
# PASO 3: RECALCULAR INVENTARIO INTERNO PROYECTADO (12 MESES, SIN NEGATIVOS)
# ─────────────────────────────────────────────


def rebuild_internal_inventory_constrained(
    session: Session,
    mes_cierre_date_id: int,
) -> int:
    """
    Recalcula el inventario interno proyectado para los 12 meses, usando:
    - Inventario inicial interno,
    - Sales In Unconstrained agregadas por SKU,
    - Llegadas de transito,
    - Llegadas de POs (fact_po_interno).

    En este recalculo NO se permiten inventarios negativos (se trunca a 0).
    Sobrescribe fact_inventory_internal_constrained.

    Returns:
        Numero de registros insertados.
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)
    sku_ids = _get_skus_with_demand(session, projection_ids)

    # Limpiar datos previos
    session.query(FactInventoryInternalConstrained).filter(
        FactInventoryInternalConstrained.date_id.in_(projection_ids),
    ).delete(synchronize_session="fetch")

    # Pre-cargar PO arrivals por SKU
    po_rows = (
        session.query(
            FactPoInterno.sku_id,
            FactPoInterno.date_id_llegada,
            func.sum(FactPoInterno.unidades_po).label("total"),
        )
        .group_by(FactPoInterno.sku_id, FactPoInterno.date_id_llegada)
        .all()
    )
    po_by_sku_month: dict[tuple[int, int], float] = {}
    for r in po_rows:
        po_by_sku_month[(r.sku_id, r.date_id_llegada)] = float(r.total or 0)

    inserted = 0

    for sku_id in sku_ids:
        inv_int_0 = _get_internal_inventory_initial(session, sku_id, mes_cierre_date_id)
        demand = _get_demand_by_month(session, sku_id, projection_ids)
        arrivals = _get_transit_arrivals_by_month(session, sku_id, projection_ids)

        inv_prev = inv_int_0

        for t in range(len(projection_ids)):
            date_id_t = projection_ids[t]
            demand_t = demand.get(date_id_t, 0.0)
            arrivals_t = arrivals.get(date_id_t, 0.0)
            arrivals_po_t = po_by_sku_month.get((sku_id, date_id_t), 0.0)

            inv_t = inv_prev - demand_t + arrivals_t + arrivals_po_t

            # No permitir negativos
            if inv_t < 0:
                inv_t = 0.0

            session.add(FactInventoryInternalConstrained(
                date_id=date_id_t,
                sku_id=sku_id,
                inventario_final_int=float(round(inv_t, 4)),
            ))
            inserted += 1
            inv_prev = inv_t

    session.flush()
    logger.info(
        "rebuild_internal_inventory_constrained: %d registros para %d SKUs",
        inserted, len(sku_ids),
    )
    return inserted


# ─────────────────────────────────────────────
# PASO 4: SALES IN CONSTRAINED + LOST SALES
# ─────────────────────────────────────────────


def compute_sales_in_constrained(
    session: Session,
    mes_cierre_date_id: int,
) -> dict:
    """
    Calcula Sales In Constrained para todos los meses de proyeccion:
    - Meses 1..L: limitadas por inventario interno disponible por SKU.
      Si el stock se agota, se asigna proporcionalmente entre clientes.
      Meses posteriores al OOS en la ventana → SI constrained = 0.
    - Meses L+1..12: Sales In Constrained = Sales In Unconstrained
      (se asume que las POs cubren la demanda).

    Tambien calcula Lost Sales por OOS irrecuperable en la ventana 1..L.

    Returns:
        dict con si_constrained_count, lost_sales_count, total_lost_sales.
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)
    sku_ids = _get_skus_with_demand(session, projection_ids)

    # Limpiar datos previos
    session.query(FactSalesInConstrained).filter(
        FactSalesInConstrained.date_id.in_(projection_ids),
    ).delete(synchronize_session="fetch")
    session.query(RptLostSalesOos).delete(synchronize_session="fetch")

    si_count = 0
    lost_count = 0
    total_lost = 0.0

    for sku_id in sku_ids:
        L = _get_lead_time(session, sku_id)
        inv_int_0 = _get_internal_inventory_initial(session, sku_id, mes_cierre_date_id)
        arrivals = _get_transit_arrivals_by_month(session, sku_id, projection_ids)

        # Inventario disponible para la ventana 1..L
        # Solo cuenta inventario inicial + transito (no POs, que llegan despues de L)
        inv_available = inv_int_0
        for t in range(min(L, len(projection_ids))):
            inv_available += arrivals.get(projection_ids[t], 0.0)

        # Obtener demanda por cliente-SKU-mes para este SKU
        si_unc_rows = (
            session.query(
                FactSalesInUnconstrained.date_id,
                FactSalesInUnconstrained.cliente_id,
                FactSalesInUnconstrained.unidades_sales_in_unc,
            )
            .filter(
                FactSalesInUnconstrained.sku_id == sku_id,
                FactSalesInUnconstrained.date_id.in_(projection_ids),
            )
            .all()
        )

        # Organizar por mes y cliente
        demand_by_month_client: dict[int, list[tuple[int, float]]] = {}
        for r in si_unc_rows:
            demand_by_month_client.setdefault(r.date_id, []).append(
                (r.cliente_id, float(r.unidades_sales_in_unc or 0))
            )

        # Calcular inventario disponible progresivo para ventana 1..L
        inv_running = inv_int_0
        oos_triggered = False
        lost_by_month: dict[int, float] = {}

        for t in range(len(projection_ids)):
            date_id_t = projection_ids[t]
            is_lt_window = t < L  # dentro de la ventana de lead time

            if is_lt_window:
                # Agregar llegadas de transito de este mes
                inv_running += arrivals.get(date_id_t, 0.0)

                if oos_triggered:
                    # Ya se agoto el stock: todo es lost sale, constrained = 0
                    for cid, d_ct in demand_by_month_client.get(date_id_t, []):
                        session.add(FactSalesInConstrained(
                            date_id=date_id_t,
                            cliente_id=cid,
                            sku_id=sku_id,
                            unidades_sales_in_constr=0.0,
                        ))
                        si_count += 1
                    # Lost sales = toda la demanda del mes
                    month_demand = sum(d for _, d in demand_by_month_client.get(date_id_t, []))
                    lost_by_month[date_id_t] = month_demand
                    continue

                # Demanda total del mes
                month_entries = demand_by_month_client.get(date_id_t, [])
                demand_total = sum(d for _, d in month_entries)

                if demand_total <= 0:
                    # Sin demanda, insertar 0 para cada cliente
                    for cid, d_ct in month_entries:
                        session.add(FactSalesInConstrained(
                            date_id=date_id_t,
                            cliente_id=cid,
                            sku_id=sku_id,
                            unidades_sales_in_constr=0.0,
                        ))
                        si_count += 1
                    continue

                if inv_running >= demand_total:
                    # Stock suficiente: constrained = unconstrained
                    for cid, d_ct in month_entries:
                        session.add(FactSalesInConstrained(
                            date_id=date_id_t,
                            cliente_id=cid,
                            sku_id=sku_id,
                            unidades_sales_in_constr=float(round(d_ct, 4)),
                        ))
                        si_count += 1
                    inv_running -= demand_total
                else:
                    # Stock-out parcial: asignar proporcionalmente
                    ratio = inv_running / demand_total if demand_total > 0 else 0.0
                    lost_this_month = 0.0

                    for cid, d_ct in month_entries:
                        constrained = d_ct * ratio
                        lost = d_ct - constrained
                        lost_this_month += lost

                        session.add(FactSalesInConstrained(
                            date_id=date_id_t,
                            cliente_id=cid,
                            sku_id=sku_id,
                            unidades_sales_in_constr=float(round(constrained, 4)),
                        ))
                        si_count += 1

                    lost_by_month[date_id_t] = lost_this_month
                    inv_running = 0.0
                    oos_triggered = True  # Meses posteriores en ventana LT: irrecuperables

            else:
                # Meses L+1..12: Constrained = Unconstrained
                for cid, d_ct in demand_by_month_client.get(date_id_t, []):
                    session.add(FactSalesInConstrained(
                        date_id=date_id_t,
                        cliente_id=cid,
                        sku_id=sku_id,
                        unidades_sales_in_constr=float(round(d_ct, 4)),
                    ))
                    si_count += 1

        # Insertar Lost Sales report
        total_lost_sku = sum(lost_by_month.values())
        if total_lost_sku > 0:
            lt_window_end = min(L, len(projection_ids)) - 1
            detalles = json.dumps({
                str(did): round(lost, 2) for did, lost in lost_by_month.items()
            })
            session.add(RptLostSalesOos(
                sku_id=sku_id,
                date_id_inicio_lt=projection_ids[0],
                date_id_fin_lt=projection_ids[lt_window_end],
                lost_sales_total=float(round(total_lost_sku, 4)),
                detalles=detalles,
            ))
            lost_count += 1
            total_lost += total_lost_sku

    session.flush()
    logger.info(
        "compute_sales_in_constrained: %d registros SI constrained, "
        "%d SKUs con lost sales (total: %.0f unidades)",
        si_count, lost_count, total_lost,
    )
    return {
        "si_constrained_count": si_count,
        "lost_sales_skus": lost_count,
        "total_lost_sales": round(total_lost, 4),
    }


# ─────────────────────────────────────────────
# ORQUESTADOR DE FASE 3
# ─────────────────────────────────────────────


def run_constraint_process(
    session: Session,
    mes_cierre_date_id: int,
) -> dict:
    """
    Fase 3: Constraint Demand para un mes de cierre dado.

    Pre-condiciones:
    - Fase 2 (Unconstrained Demand) ya se ejecuto para mes_cierre_date_id
      y existen registros en fact_sales_in_unconstrained para los 12 meses
      proyectados.
    - Inventario interno e inventario en transito del mes de cierre y meses
      futuros ya fueron cargados.

    Pasos:
    1) Simular inventario interno ventana 1..L permitiendo negativos.
    2) Generar POs por SKU (fact_po_interno).
    3) Recalcular inventario interno proyectado 12 meses sin negativos
       (fact_inventory_internal_constrained).
    4) Calcular Sales In Constrained meses 1..L y lost sales por OOS.
    5) Igualar Sales In Constrained = Sales In Unconstrained en meses L+1..12
       (hecho dentro del paso 4).
    6) Retornar resumen.

    Args:
        session: Sesion de SQLAlchemy activa.
        mes_cierre_date_id: date_id del ultimo mes historico (YYYYMM).

    Returns:
        dict con resumen del proceso.
    """
    start_time = datetime.now()
    summary = {
        "mes_cierre_date_id": mes_cierre_date_id,
        "fase": "CONSTRAINT_DEMAND",
        "estado": "EN_PROCESO",
        "pasos": {},
    }

    try:
        # Paso 1: Simular inventario interno en ventana 1..L
        logger.info("Constraint Paso 1: Simulando inventario interno ventana 1..L...")
        sim_count = simulate_internal_inventory_window(session, mes_cierre_date_id)
        summary["pasos"]["1_simulacion_lt"] = {"registros": sim_count}

        # Paso 2: Generar POs
        logger.info("Constraint Paso 2: Generando plan de POs...")
        po_count = generate_po_plan(session, mes_cierre_date_id)
        summary["pasos"]["2_pos_generadas"] = {"pos": po_count}

        # Paso 3: Recalcular inventario interno 12 meses con POs
        logger.info("Constraint Paso 3: Recalculando inventario interno con POs...")
        inv_count = rebuild_internal_inventory_constrained(session, mes_cierre_date_id)
        summary["pasos"]["3_inventario_reconstruido"] = {"registros": inv_count}

        # Paso 4: Sales In Constrained + Lost Sales
        logger.info("Constraint Paso 4: Calculando Sales In Constrained y Lost Sales...")
        constr_result = compute_sales_in_constrained(session, mes_cierre_date_id)
        summary["pasos"]["4_sales_in_constrained"] = constr_result

        summary["estado"] = "COMPLETADO"
        summary["mensaje"] = (
            f"Constraint Demand completado. "
            f"{po_count} POs generadas, "
            f"{constr_result['lost_sales_skus']} SKUs con lost sales "
            f"(total: {constr_result['total_lost_sales']:,.0f} unidades)."
        )
        summary["duracion_segundos"] = (datetime.now() - start_time).total_seconds()

        session.commit()
        logger.info("run_constraint_process completado para mes %d", mes_cierre_date_id)
        return summary

    except Exception as e:
        session.rollback()
        summary["estado"] = "ERROR"
        summary["mensaje"] = f"Error en Constraint Demand: {str(e)}"
        summary["duracion_segundos"] = (datetime.now() - start_time).total_seconds()
        logger.exception("Error en run_constraint_process para mes %d", mes_cierre_date_id)
        raise
