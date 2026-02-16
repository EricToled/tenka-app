"""
constraint_demand.py — Fase 3: Constraint Demand.

Refactor Fase A v2 (feb 2026) — Correcciones post-auditoria de inconsistencias:
    P1: Inventario teorico se calcula 1..12 (no solo 1..L).
    P2: POs con logica secuencial PO1..PO(L+1) por ventanas de L meses.
    P3: Inventario final Constraint: periodos 1..L truncados a 0 despues de PO1;
        periodos L+1..12 negativos = error de proceso (NO truncar).
    I1: Asignacion de inventario limitado: secuencial por tamaño (mayor a menor).
    I2: Sin cascada oos_triggered; cada mes se evalua independiente con inv_running.

Calcula, para cada SKU y horizonte de 12 meses de proyeccion:
    1. Inventario interno teorico 1..12 (permite negativos, en memoria).
    2. POs secuenciales: PO1..PO(L+1) con ventanas de L meses de demanda.
    3. Inventario interno final Constraint 1..12.
    4. Sales In Constrained (limitadas por stock interno en ventana 1..L).
    5. Lost Sales por OOS en ventana de lead time.

Invariantes:
    - Todos los calculos de inventario interno son a nivel SKU global (no por cliente).
    - Fase 3 NO recalcula ni toca Sales Out (eso es Fase B).
    - Lead time por SKU (DimSku.lead_time_meses), default 4.

Reglas de truncamiento:
    - Inventario teorico periodos 1..L: truncar negativos a 0 SOLO despues de PO1.
    - Inventario final Constraint periodos 1..L: = inv_teo truncado (pueden ser 0).
    - Inventario final Constraint periodos L+1..12: negativo = ERROR de proceso.

Flujo de proceso (Fase A — solo hasta Sales In Constraint):
    run_constraint_process → genera POs, inventario constraint, SI constrained.
    Despues de esto, el sistema genera un reporte de asignacion editable (gate bloqueante).
    Solo tras aprobacion del usuario se ejecuta Fase B (Sales Out Constraint).
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
    """
    Obtiene el lead time en meses para un SKU. Default 4.
    Loguea warning si el SKU no tiene lead_time_meses definido.
    """
    sku = session.get(DimSku, sku_id)
    if sku and sku.lead_time_meses:
        return sku.lead_time_meses
    logger.warning(
        "SKU %d sin lead_time_meses definido, usando default L=4.", sku_id
    )
    return 4


# ─────────────────────────────────────────────
# PASO 1: INVENTARIO INTERNO TEORICO 1..12 (EN MEMORIA)
# ─────────────────────────────────────────────


def _compute_theoretical_inventory(
    inv_hist: float,
    demand: dict[int, float],
    arrivals: dict[int, float],
    projection_ids: list[int],
) -> list[float]:
    """
    Calcula inventario interno teorico para los 12 periodos de proyeccion.
    Solo usa inventario inicial + demanda SI_unc + llegadas de transito.
    NO incluye POs. SE PERMITEN NEGATIVOS.

    Formulas:
        inv_teo[1] = inv_hist - SI_unc_1 + Arrivals_1
        inv_teo[t] = inv_teo[t-1] - SI_unc_t + Arrivals_t   (t=2..12)

    Returns:
        Lista de 12 floats con inventario teorico (puede contener negativos).
    """
    inv_teo = [0.0] * len(projection_ids)
    inv_prev = inv_hist

    for t in range(len(projection_ids)):
        did = projection_ids[t]
        d = demand.get(did, 0.0)
        a = arrivals.get(did, 0.0)
        inv_teo[t] = inv_prev - d + a
        inv_prev = inv_teo[t]

    return inv_teo


# ─────────────────────────────────────────────
# PASO 2: CALCULO DE POs SECUENCIALES + INVENTARIO FINAL CONSTRAINT
# ─────────────────────────────────────────────


def _compute_pos_and_final_inventory(
    inv_teo: list[float],
    demand: dict[int, float],
    arrivals: dict[int, float],
    projection_ids: list[int],
    L: int,
) -> tuple[list[dict], list[float]]:
    """
    Calcula POs secuenciales PO1..PO(L+1) y el inventario final Constraint 1..12.

    Logica detallada:

    1) PO1 (orden en t=1, llega en t=L+1):
       R1 = inv_teo[L-1] - sum(SI_unc[L+1..2L]) + Arrivals[L+1]
       Si R1 < 0: PO1 = |R1|, sino PO1 = 0.

    2) Truncamiento unico permitido:
       Despues de PO1, para t=0..L-1: si inv_teo[t] < 0 → forzar a 0.

    3) Inventario real del periodo L+1:
       inv_real[L] = inv_teo[L-1]_truncado - SI_unc[L+1] + Arrivals[L+1] + PO1

    4) PO2..PO(L+1) secuenciales:
       POk (k=2..L+1): orden en k, llega en k+L.
       base = inv_real[periodo anterior], ventana = L meses de demanda.
       Rk = base - ventana + Arrivals[llegada]. Si Rk < 0: POk = |Rk|.

    5) Periodos restantes: inv_real[t] = inv_real[t-1] - SI[t] + Arrivals[t].

    Returns:
        Tupla de:
        - Lista de dicts con POs: [{date_id_orden, date_id_llegada, unidades_po}]
        - Lista de 12 floats: inventario final Constraint.
          Periodos 1..L: truncados a 0 (pueden ser 0).
          Periodos L+1..12: si negativo = error de proceso.
    """
    n = len(projection_ids)
    pos: list[dict] = []
    inv_final = [0.0] * n

    # ── Paso 1: Calcular PO1 ──
    po1_units = 0.0
    if L < n:
        base_po1 = inv_teo[L - 1]
        demand_window_end = min(L + L, n)
        demand_window = sum(
            demand.get(projection_ids[j], 0.0)
            for j in range(L, demand_window_end)
        )
        arrivals_at_arrival = arrivals.get(projection_ids[L], 0.0)
        r1 = base_po1 - demand_window + arrivals_at_arrival

        if r1 < -0.5:
            po1_units = abs(r1)
            pos.append({
                "date_id_orden": projection_ids[0],
                "date_id_llegada": projection_ids[L],
                "unidades_po": round(po1_units, 4),
            })

    # ── Paso 2: Truncamiento permitido periodos 1..L ──
    inv_teo_truncated = inv_teo.copy()
    for t in range(min(L, n)):
        if inv_teo_truncated[t] < 0:
            inv_teo_truncated[t] = 0.0

    # ── Paso 3: Inventario final periodos 1..L = truncado ──
    for t in range(min(L, n)):
        inv_final[t] = inv_teo_truncated[t]

    # ── Paso 4: Inventario real periodo L+1 (post PO1) ──
    if L < n:
        did_arrival = projection_ids[L]
        inv_final[L] = (
            inv_teo_truncated[L - 1]
            - demand.get(did_arrival, 0.0)
            + arrivals.get(did_arrival, 0.0)
            + po1_units
        )

    # ── Paso 5: PO2..PO(L+1) secuenciales ──
    for k in range(2, L + 2):
        arrival_idx = L + (k - 1)
        order_idx = k - 1

        if arrival_idx >= n:
            break

        base = inv_final[arrival_idx - 1]

        demand_start_idx = arrival_idx
        demand_end_idx = min(arrival_idx + L, n)
        demand_window = sum(
            demand.get(projection_ids[j], 0.0)
            for j in range(demand_start_idx, demand_end_idx)
        )

        arrivals_at = arrivals.get(projection_ids[arrival_idx], 0.0)
        rk = base - demand_window + arrivals_at

        pok_units = 0.0
        if rk < -0.5:
            pok_units = abs(rk)
            pos.append({
                "date_id_orden": projection_ids[order_idx],
                "date_id_llegada": projection_ids[arrival_idx],
                "unidades_po": round(pok_units, 4),
            })

        did_arrival = projection_ids[arrival_idx]
        inv_final[arrival_idx] = (
            base
            - demand.get(did_arrival, 0.0)
            + arrivals.get(did_arrival, 0.0)
            + pok_units
        )

    # ── Paso 6: Completar periodos restantes ──
    last_calculated = min(L, n) - 1
    if L < n:
        last_calculated = L
    for k in range(2, L + 2):
        arrival_idx = L + (k - 1)
        if arrival_idx < n:
            last_calculated = max(last_calculated, arrival_idx)

    for t in range(last_calculated + 1, n):
        did = projection_ids[t]
        inv_final[t] = (
            inv_final[t - 1]
            - demand.get(did, 0.0)
            + arrivals.get(did, 0.0)
        )

    return pos, inv_final


def generate_po_plan(
    session: Session,
    mes_cierre_date_id: int,
) -> int:
    """
    Genera POs internas por SKU + persiste inventario final Constraint 1..12.

    Para cada SKU:
    1. Calcula inventario teorico 1..12.
    2. Calcula PO1..PO(L+1) con ventanas de L meses.
    3. Persiste POs en fact_po_interno.
    4. Persiste inventario final Constraint en fact_inventory_internal_constrained.
       Periodos 1..L: truncados a 0 (permitido).
       Periodos L+1..12: si negativo, reporta ERROR (no trunca).

    Returns:
        Numero de POs insertadas.
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)
    sku_ids = _get_skus_with_demand(session, projection_ids)

    # Limpiar POs previas y inventario constraint previo
    session.query(FactPoInterno).delete(synchronize_session="fetch")
    session.query(FactInventoryInternalConstrained).filter(
        FactInventoryInternalConstrained.date_id.in_(projection_ids),
    ).delete(synchronize_session="fetch")

    po_count = 0
    inv_count = 0
    negative_errors: list[dict] = []

    for sku_id in sku_ids:
        L = _get_lead_time(session, sku_id)
        inv_hist = _get_internal_inventory_initial(session, sku_id, mes_cierre_date_id)
        demand = _get_demand_by_month(session, sku_id, projection_ids)
        arrivals = _get_transit_arrivals_by_month(session, sku_id, projection_ids)

        inv_teo = _compute_theoretical_inventory(
            inv_hist, demand, arrivals, projection_ids
        )
        pos, inv_final = _compute_pos_and_final_inventory(
            inv_teo, demand, arrivals, projection_ids, L
        )

        # Persistir POs
        for po in pos:
            session.add(FactPoInterno(
                sku_id=sku_id,
                date_id_orden=po["date_id_orden"],
                date_id_llegada=po["date_id_llegada"],
                unidades_po=float(po["unidades_po"]),
            ))
            po_count += 1

        # Persistir inventario final Constraint
        for t in range(len(projection_ids)):
            inv_val = inv_final[t]

            # Periodos L+1..12: negativo = ERROR de proceso (no truncar)
            if t >= L and inv_val < -0.5:
                negative_errors.append({
                    "sku_id": sku_id,
                    "date_id": projection_ids[t],
                    "periodo": t + 1,
                    "inventario": round(inv_val, 4),
                })
                logger.error(
                    "ERROR DE PROCESO: Inventario final Constraint negativo "
                    "en periodo %d (date_id=%d) para SKU=%d: %.4f. "
                    "Periodos L+1..12 no deben ser negativos.",
                    t + 1, projection_ids[t], sku_id, inv_val,
                )

            session.add(FactInventoryInternalConstrained(
                date_id=projection_ids[t],
                sku_id=sku_id,
                inventario_final_int=float(round(inv_val, 4)),
            ))
            inv_count += 1

    session.flush()

    if negative_errors:
        logger.error(
            "generate_po_plan: %d periodos con inventario negativo (post-PO). "
            "Detalle: %s",
            len(negative_errors),
            json.dumps(negative_errors[:20]),
        )

    logger.info(
        "generate_po_plan: %d POs generadas, %d registros inventario, "
        "%d SKUs (%d errores inv negativo post-L)",
        po_count, inv_count, len(sku_ids), len(negative_errors),
    )
    return po_count


# ─────────────────────────────────────────────
# FUNCIONES LEGACY (no-op, compatibilidad con orquestador)
# ─────────────────────────────────────────────


def simulate_internal_inventory_window(
    session: Session,
    mes_cierre_date_id: int,
) -> int:
    """No-op. Inventario teorico se calcula en memoria dentro de generate_po_plan."""
    logger.info("simulate_internal_inventory_window: No-op (calculo en memoria).")
    return 0


def rebuild_internal_inventory_constrained(
    session: Session,
    mes_cierre_date_id: int,
) -> int:
    """No-op. Inventario Constraint ya persistido por generate_po_plan."""
    projection_ids = _get_projection_ids(mes_cierre_date_id)
    count = (
        session.query(FactInventoryInternalConstrained)
        .filter(FactInventoryInternalConstrained.date_id.in_(projection_ids))
        .count()
    )
    logger.info(
        "rebuild_internal_inventory_constrained: %d registros ya presentes.", count
    )
    return count


# ─────────────────────────────────────────────
# PASO 4: SALES IN CONSTRAINED + LOST SALES
# ─────────────────────────────────────────────
#
# Reglas clave (post-auditoria I1, I2):
#   - Asignacion de inventario limitado: SECUENCIAL POR TAMAÑO.
#     Se ordenan clientes de mayor a menor demanda SI_unc.
#     Se llena cada cliente completo hasta agotar inventario.
#     El ultimo cliente puede recibir asignacion parcial.
#   - Sin cascada oos_triggered: cada mes se evalua independiente.
#     Si hay llegadas de transito en un mes posterior, estas se
#     suman al inv_running y pueden reabrir asignacion.
# ─────────────────────────────────────────────


def _allocate_sequential_by_size(
    entries: list[tuple[int, float]],
    available: float,
) -> list[tuple[int, float, float]]:
    """
    Asigna inventario disponible a clientes de forma secuencial por tamaño.

    Ordena clientes de mayor a menor demanda SI_unc.
    Llena cada cliente completo hasta agotar inventario.
    El ultimo cliente puede recibir parcial. Los restantes reciben 0.

    Ejemplo (instrucciones originales):
        Inventario = 60
        Clientes: A=30, B=20, C=15, D=5 (total demanda = 70)
        Resultado: A=30, B=20, C=10, D=0

    Args:
        entries: Lista de (cliente_id, demanda_si_unc).
        available: Inventario disponible.

    Returns:
        Lista de (cliente_id, asignado, lost) para cada cliente.
    """
    if available <= 0:
        return [(cid, 0.0, d) for cid, d in entries]

    # Ordenar de mayor a menor demanda
    sorted_entries = sorted(entries, key=lambda x: x[1], reverse=True)

    result: list[tuple[int, float, float]] = []
    remaining = available

    for cid, demand_c in sorted_entries:
        if remaining >= demand_c:
            # Cliente completo
            result.append((cid, demand_c, 0.0))
            remaining -= demand_c
        elif remaining > 0:
            # Cliente parcial (ultimo con inventario)
            result.append((cid, remaining, demand_c - remaining))
            remaining = 0.0
        else:
            # Sin inventario
            result.append((cid, 0.0, demand_c))

    return result


def compute_sales_in_constrained(
    session: Session,
    mes_cierre_date_id: int,
) -> dict:
    """
    Calcula Sales In Constrained para todos los meses de proyeccion.

    Logica:
    - Periodos L+1..12: SI_constr = SI_unc (POs cubren la demanda).

    - Periodos 1..L: cada mes se evalua INDEPENDIENTE con inv_running.
        inv_running empieza en inv_hist.
        Cada mes: inv_running += arrivals[t] (incluso si meses previos agotaron stock,
        las llegadas de transito pueden reabrir asignacion).

        Si inv_running >= demanda_total: SI_constr_c = SI_unc_c para todos.
        Si 0 < inv_running < demanda_total: asignacion SECUENCIAL POR TAMAÑO.
        Si inv_running <= 0: SI_constr = 0, todo es lost.

    Returns:
        dict con si_constrained_count, lost_sales_skus, total_lost_sales.
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

        # Obtener demanda por cliente-SKU-mes
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

        # Organizar: {date_id: [(cliente_id, demanda), ...]}
        demand_by_month_client: dict[int, list[tuple[int, float]]] = {}
        for r in si_unc_rows:
            demand_by_month_client.setdefault(r.date_id, []).append(
                (r.cliente_id, float(r.unidades_sales_in_unc or 0))
            )

        # ── Inventario running progresivo, mes a mes, SIN cascada ──
        inv_running = inv_int_0
        lost_by_month: dict[int, float] = {}

        for t in range(len(projection_ids)):
            date_id_t = projection_ids[t]
            is_lt_window = t < L

            if is_lt_window:
                # Sumar llegadas de transito de este mes
                # (I2: siempre se suman, incluso si meses previos agotaron stock)
                inv_running += arrivals.get(date_id_t, 0.0)

                month_entries = demand_by_month_client.get(date_id_t, [])
                demand_total = sum(d for _, d in month_entries)

                if demand_total <= 0:
                    # Sin demanda: insertar 0 para cada cliente
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

                elif inv_running > 0:
                    # Stock parcial: ASIGNACION SECUENCIAL POR TAMAÑO (I1)
                    allocation = _allocate_sequential_by_size(
                        month_entries, inv_running
                    )
                    lost_this_month = 0.0

                    for cid, assigned, lost in allocation:
                        session.add(FactSalesInConstrained(
                            date_id=date_id_t,
                            cliente_id=cid,
                            sku_id=sku_id,
                            unidades_sales_in_constr=float(round(assigned, 4)),
                        ))
                        si_count += 1
                        lost_this_month += lost

                    lost_by_month[date_id_t] = lost_this_month
                    inv_running = 0.0
                    # I2: NO se activa oos_triggered. El proximo mes se evalua
                    # independiente con su inv_running (que puede recibir arrivals).

                else:
                    # inv_running <= 0: todo es lost para este mes
                    for cid, d_ct in month_entries:
                        session.add(FactSalesInConstrained(
                            date_id=date_id_t,
                            cliente_id=cid,
                            sku_id=sku_id,
                            unidades_sales_in_constr=0.0,
                        ))
                        si_count += 1
                    lost_by_month[date_id_t] = demand_total
                    # I2: NO cascade. Siguiente mes se evalua con su inv_running.

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

        # ── Insertar reporte de Lost Sales ──
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
# ORQUESTADOR DE FASE 3 (firma publica sin cambios)
# ─────────────────────────────────────────────


def run_constraint_process(
    session: Session,
    mes_cierre_date_id: int,
) -> dict:
    """
    Fase 3: Constraint Demand para un mes de cierre dado.

    Pre-condiciones:
    - Fase 2 (Unconstrained Demand) ya ejecutada.
    - Inventario interno e inventario en transito cargados.

    Pasos:
    1) [No-op] Inventario teorico en memoria.
    2) Generar POs secuenciales + persistir inventario Constraint 1..12.
    3) [No-op] Inventario ya en paso 2.
    4) Calcular Sales In Constrained (secuencial por tamaño) + Lost Sales.

    NOTA: Despues de este proceso, se debe generar el reporte de asignacion
    editable y esperar aprobacion del usuario antes de ejecutar Fase B
    (Sales Out Constraint).

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
        # Paso 1: [No-op]
        logger.info("Constraint Paso 1: Inventario teorico (en memoria, no-op)...")
        sim_count = simulate_internal_inventory_window(session, mes_cierre_date_id)
        summary["pasos"]["1_simulacion_lt"] = {"registros": sim_count}

        # Paso 2: POs + inventario Constraint
        logger.info("Constraint Paso 2: POs secuenciales + inventario Constraint...")
        po_count = generate_po_plan(session, mes_cierre_date_id)
        summary["pasos"]["2_pos_generadas"] = {"pos": po_count}

        # Paso 3: [No-op]
        logger.info("Constraint Paso 3: Inventario (ya en paso 2)...")
        inv_count = rebuild_internal_inventory_constrained(session, mes_cierre_date_id)
        summary["pasos"]["3_inventario_reconstruido"] = {"registros": inv_count}

        # Paso 4: Sales In Constrained + Lost Sales
        logger.info("Constraint Paso 4: Sales In Constrained (secuencial) + Lost Sales...")
        constr_result = compute_sales_in_constrained(session, mes_cierre_date_id)
        summary["pasos"]["4_sales_in_constrained"] = constr_result

        summary["estado"] = "COMPLETADO_PENDIENTE_APROBACION"
        summary["mensaje"] = (
            f"Constraint Demand Fase A completado. "
            f"{po_count} POs generadas, "
            f"{constr_result['lost_sales_skus']} SKUs con lost sales "
            f"(total: {constr_result['total_lost_sales']:,.0f} unidades). "
            f"PENDIENTE: Aprobacion de reporte de asignacion antes de Fase B."
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
