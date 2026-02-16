"""
constraint_demand.py — Fase 3: Constraint Demand.

Refactor v3 (feb 2026) — Rewrite per Agent Execution Plan v2:
    §3.3: Inventario teorico 1..12 permite negativos, in-memory only.
    §3.4: POs secuenciales PO1..PO5 con ventanas de L meses de demanda.
         Post-PO1 truncation only on periods 0..L-1.
    §4:   Inventario final Constraint: periodos 1..L truncados, L+1..12 negativo = ERROR.
    §3.5: Sales In Constrained: sequential by size DESC, no oos_triggered cascade,
         arrivals added per-month only (not pre-summed).
    §6:   Gate: allocation report, validation (cumulative), apply edits.

Convenciones:
    - Round all numeric values to 4 decimals before DB storage.
    - Never call session.commit() inside business logic. Only API endpoints commit.
    - Use session.flush() after batch inserts.
    - Variable L = _get_lead_time(session, sku_id) (default 4). Do not hardcode 4.
    - projection_ids = list of 12 date_id values (months 1-12 after close).
"""

import json
import logging
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from .eligibility import _generate_date_id_range
from .models import (
    ConstraintProcessControl,
    DimCliente,
    DimSku,
    FactInventarioInterno,
    FactInventarioTransito,
    FactInventoryInternalConstrained,
    FactPoInterno,
    FactSalesInConstrained,
    FactSalesInUnconstrained,
    FactSalesOutConstrained,
    FactInventoryClienteConstrained,
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
    """
    sku = session.get(DimSku, sku_id)
    if sku and sku.lead_time_meses:
        return sku.lead_time_meses
    logger.warning(
        "SKU %d sin lead_time_meses definido, usando default L=4.", sku_id
    )
    return 4


# ─────────────────────────────────────────────
# STEP 2: INVENTARIO TEORICO 1..12 (§3.3)
# ─────────────────────────────────────────────


def compute_theoretical_inventory(
    session: Session,
    mes_cierre_date_id: int,
) -> dict[int, dict[int, float]]:
    """
    Calcula inventario teorico para TODOS los SKUs, 12 periodos de proyeccion.

    Formulas (§3.3):
        inv_teo[1] = inv_hist - SI_unc[1] + Arrivals[1]  # §3.3: negatives allowed
        inv_teo[t] = inv_teo[t-1] - SI_unc[t] + Arrivals[t]  # §3.3: negatives allowed

    Negativos permitidos. NO persiste a DB — retorna en memoria.

    Returns:
        {sku_id: {date_id: inv_teo_value}} for all 12 projection months.
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)
    sku_ids = _get_skus_with_demand(session, projection_ids)

    inv_teo_all: dict[int, dict[int, float]] = {}

    for sku_id in sku_ids:
        inv_hist = _get_internal_inventory_initial(session, sku_id, mes_cierre_date_id)
        demand = _get_demand_by_month(session, sku_id, projection_ids)
        arrivals = _get_transit_arrivals_by_month(session, sku_id, projection_ids)

        inv_teo: dict[int, float] = {}
        inv_prev = inv_hist

        for t in range(len(projection_ids)):
            did = projection_ids[t]
            d = demand.get(did, 0.0)
            a = arrivals.get(did, 0.0)
            inv_teo[did] = round(inv_prev - d + a, 4)  # §3.3: negatives allowed
            inv_prev = inv_teo[did]

        inv_teo_all[sku_id] = inv_teo

    logger.info(
        "compute_theoretical_inventory: %d SKUs, 12 months each",
        len(sku_ids),
    )
    return inv_teo_all


# ─────────────────────────────────────────────
# STEP 3: PO PLAN + INVENTARIO FINAL (§3.4)
# ─────────────────────────────────────────────


def generate_po_plan(
    session: Session,
    mes_cierre_date_id: int,
    inv_teo_all: dict[int, dict[int, float]],
) -> tuple[int, dict[int, dict[int, float]]]:
    """
    Genera POs internas por SKU + calcula inventario final para 12 periodos.

    Math (§3.4):
    PO1 (order idx 0, arrives idx L):
        S1 = sum SI_unc[L..L+3], clamped to < 12
        R1 = inv_teo[L-1] - S1 + Arrivals[L]
        PO1 = |R1| if R1 < 0, else 0

    Post-PO1 truncation (§3.4 — ONLY allowed truncation):
        For t = 0..L-1: if inv_teo[t] < 0 → set to 0

    Chained inventory starting at period L:
        inv[L] = inv_teo_truncated[L-1] - SI_unc[L] + Arrivals[L] + PO1

    PO_k for k=2..5 (order idx k-1, arrives idx k-1+L):
        base = inv[prev arrival idx]
        demand_window = sum SI_unc[j] for j=(k-1+L)..(k-1+2L-1), clamped < 12
        R_k = base - demand_window + Arrivals[k-1+L]
        PO_k = |R_k| if R_k < 0, else 0

    Continuation (after last PO arrival):
        inv[t] = inv[t-1] - SI_unc[t] + Arrivals[t]

    Returns:
        (po_count, inv_final_all) where inv_final_all = {sku_id: {date_id: inv_value}}
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)
    n = len(projection_ids)  # always 12

    # Delete existing POs
    session.query(FactPoInterno).delete(synchronize_session="fetch")

    po_count = 0
    inv_final_all: dict[int, dict[int, float]] = {}

    for sku_id, inv_teo_dict in inv_teo_all.items():
        L = _get_lead_time(session, sku_id)
        demand = _get_demand_by_month(session, sku_id, projection_ids)
        arrivals = _get_transit_arrivals_by_month(session, sku_id, projection_ids)

        # Convert inv_teo dict to indexed list for easier manipulation
        p = projection_ids  # shorthand
        inv_teo = [inv_teo_dict.get(p[t], 0.0) for t in range(n)]
        si_unc = [demand.get(p[t], 0.0) for t in range(n)]
        arr = [arrivals.get(p[t], 0.0) for t in range(n)]

        inv_final = [0.0] * n
        pos_for_sku: list[dict] = []

        # ── PO1: order period 1 (idx 0), arrives period L+1 (idx L) ──
        po1_units = 0.0
        if L < n:
            # §3.4: S1 = SI_unc for 4 months starting at arrival period
            s1_end = min(L + L, n)  # §3.4: clamped to < 12
            s1 = sum(si_unc[j] for j in range(L, s1_end))  # §3.4: demand window
            r1 = inv_teo[L - 1] - s1 + arr[L]  # §3.4: R1 formula
            if r1 < 0:
                po1_units = abs(r1)  # §3.4: PO1 = |R1| if R1 < 0
                pos_for_sku.append({
                    "date_id_orden": p[0],
                    "date_id_llegada": p[L],
                    "unidades_po": round(po1_units, 4),
                })

        # ── Post-PO1 truncation: ONLY for periods 0..L-1 (§3.4) ──
        inv_teo_trunc = inv_teo.copy()
        for t in range(min(L, n)):
            if inv_teo_trunc[t] < 0:
                inv_teo_trunc[t] = 0.0  # §3.4: ONLY allowed truncation

        # ── Store periods 1..L as truncated values ──
        for t in range(min(L, n)):
            inv_final[t] = inv_teo_trunc[t]

        # ── Chained inventory at arrival of PO1 (idx L) ──
        if L < n:
            inv_final[L] = round(
                inv_teo_trunc[L - 1] - si_unc[L] + arr[L] + po1_units, 4
            )  # §3.4: chained inv formula

        # ── PO_k for k=2..5 (order idx k-1, arrives idx k-1+L) ──
        for k in range(2, 6):  # k=2,3,4,5
            order_idx = k - 1
            arrival_idx = k - 1 + L

            if arrival_idx >= n:
                break

            base = inv_final[arrival_idx - 1]  # §3.4: base = inv[previous period]

            # §3.4: demand window of L months starting at arrival
            dw_start = arrival_idx
            dw_end = min(arrival_idx + L, n)  # §3.4: clamped to < 12
            demand_window = sum(si_unc[j] for j in range(dw_start, dw_end))

            rk = base - demand_window + arr[arrival_idx]  # §3.4: R_k formula
            pok_units = 0.0
            if rk < 0:
                pok_units = abs(rk)  # §3.4: PO_k = |R_k| if R_k < 0
                pos_for_sku.append({
                    "date_id_orden": p[order_idx],
                    "date_id_llegada": p[arrival_idx],
                    "unidades_po": round(pok_units, 4),
                })

            inv_final[arrival_idx] = round(
                base - si_unc[arrival_idx] + arr[arrival_idx] + pok_units, 4
            )  # §3.4: chained inv with PO

        # ── Continuation (periods after last PO arrival) ──
        # Determine last calculated index
        last_calc = min(L, n) - 1  # truncated periods
        if L < n:
            last_calc = L  # PO1 arrival
        for k in range(2, 6):
            aidx = k - 1 + L
            if aidx < n:
                last_calc = max(last_calc, aidx)

        for t in range(last_calc + 1, n):
            inv_final[t] = round(
                inv_final[t - 1] - si_unc[t] + arr[t], 4
            )  # §3.4: continuation formula

        # ── Persist POs ──
        for po in pos_for_sku:
            session.add(FactPoInterno(
                sku_id=sku_id,
                date_id_orden=po["date_id_orden"],
                date_id_llegada=po["date_id_llegada"],
                unidades_po=float(po["unidades_po"]),
            ))
            po_count += 1

        # ── Store inv_final as dict ──
        inv_final_all[sku_id] = {p[t]: inv_final[t] for t in range(n)}

    session.flush()
    logger.info(
        "generate_po_plan: %d POs generated for %d SKUs",
        po_count, len(inv_teo_all),
    )
    return po_count, inv_final_all


# ─────────────────────────────────────────────
# STEP 4: REBUILD INTERNAL INVENTORY CONSTRAINED (§4)
# ─────────────────────────────────────────────


def rebuild_internal_inventory_constrained(
    session: Session,
    mes_cierre_date_id: int,
    inv_final_all: dict[int, dict[int, float]],
) -> int:
    """
    Persiste inventario final Constraint 1..12 a DB.

    Rules (§4):
    - Periods 1..L (t < L): stored as truncated (max(val, 0.0)).
    - Periods L+1..12 (t >= L): if val < -0.01 → raise ValueError (process ERROR).

    Returns:
        Count of records inserted.
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)

    # Delete existing constrained inventory for projection periods
    session.query(FactInventoryInternalConstrained).filter(
        FactInventoryInternalConstrained.date_id.in_(projection_ids),
    ).delete(synchronize_session="fetch")

    count = 0

    for sku_id, inv_dict in inv_final_all.items():
        L = _get_lead_time(session, sku_id)

        for t in range(len(projection_ids)):
            did = projection_ids[t]
            val = inv_dict.get(did, 0.0)

            if t >= L and val < -0.01:
                # §4: negative in L+1..12 = process ERROR. Do NOT clamp to 0.
                raise ValueError(
                    f"PROCESS ERROR: Negative constrained inventory in period "
                    f"{t + 1} (date_id={did}) for SKU={sku_id}: {val:.4f}. "
                    f"Periods L+1..12 must not be negative."
                )

            if t < L:
                val = max(val, 0.0)  # §4: periods 1..L stored as truncated

            session.add(FactInventoryInternalConstrained(
                date_id=did,
                sku_id=sku_id,
                inventario_final_int=float(round(val, 4)),
            ))
            count += 1

    session.flush()
    logger.info(
        "rebuild_internal_inventory_constrained: %d records inserted for %d SKUs",
        count, len(inv_final_all),
    )
    return count


# ─────────────────────────────────────────────
# STEP 5: SALES IN CONSTRAINED + LOST SALES (§3.5)
# ─────────────────────────────────────────────


def _allocate_sequential_by_size(
    entries: list[tuple[int, float]],
    available: float,
) -> list[tuple[int, float, float]]:
    """
    Asigna inventario secuencial por tamaño (mayor a menor demanda).

    §3.5: Sort clients by demand DESC, give full demand until stock runs out.

    Returns:
        List of (cliente_id, assigned, lost) for each client.
    """
    if available <= 0:
        return [(cid, 0.0, d) for cid, d in entries]

    sorted_entries = sorted(entries, key=lambda x: x[1], reverse=True)  # §3.5: sort DESC

    result: list[tuple[int, float, float]] = []
    remaining = available

    for cid, demand_c in sorted_entries:
        if remaining >= demand_c:
            result.append((cid, demand_c, 0.0))  # §3.5: full demand
            remaining -= demand_c
        elif remaining > 0:
            result.append((cid, remaining, demand_c - remaining))  # §3.5: partial
            remaining = 0.0
        else:
            result.append((cid, 0.0, demand_c))  # §3.5: all lost

    return result


def compute_sales_in_constrained(
    session: Session,
    mes_cierre_date_id: int,
) -> dict:
    """
    Calcula Sales In Constrained para todos los meses de proyeccion.

    Periods 1..L: each month evaluated INDEPENDENTLY with inv_running.
        - §3.5 step 1: inv_running += Arrivals[t] (add only in month t, not pre-summed)
        - Case A: inv_running >= demand_total → full demand
        - Case B: 0 < inv_running < demand_total → sequential by size DESC
        - Case C: inv_running <= 0 → all zero, all lost

    Periods L+1..12: SI_constr = SI_unc (copy directly).

    Returns:
        dict with si_constrained_count, lost_sales_skus, total_lost_sales.
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)
    sku_ids = _get_skus_with_demand(session, projection_ids)

    # Clean previous data
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

        # Get demand by client-SKU-month
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

        # Organize: {date_id: [(cliente_id, demand), ...]}
        demand_by_month_client: dict[int, list[tuple[int, float]]] = {}
        for r in si_unc_rows:
            demand_by_month_client.setdefault(r.date_id, []).append(
                (r.cliente_id, float(r.unidades_sales_in_unc or 0))
            )

        # §3.5: inv_running starts at inv_hist
        inv_running = inv_int_0
        lost_records: list[tuple[int, int, float]] = []  # (cliente_id, date_id, lost_units)

        for t in range(len(projection_ids)):
            date_id_t = projection_ids[t]
            is_lt_window = t < L

            if is_lt_window:
                # §3.5 step 1: add arrivals for THIS month only (not pre-summed)
                inv_running += arrivals.get(date_id_t, 0.0)

                month_entries = demand_by_month_client.get(date_id_t, [])
                demand_total = sum(d for _, d in month_entries)

                if demand_total <= 0:
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
                    # §3.5 Case A: full demand for all clients
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
                    # §3.5 Case B: sequential by size DESC
                    allocation = _allocate_sequential_by_size(
                        month_entries, inv_running
                    )

                    for cid, assigned, lost in allocation:
                        session.add(FactSalesInConstrained(
                            date_id=date_id_t,
                            cliente_id=cid,
                            sku_id=sku_id,
                            unidades_sales_in_constr=float(round(assigned, 4)),
                        ))
                        si_count += 1
                        if lost > 0.001:
                            lost_records.append((cid, date_id_t, lost))

                    inv_running = 0.0
                    # §3.5: NO oos_triggered cascade

                else:
                    # §3.5 Case C: inv_running <= 0, all lost
                    for cid, d_ct in month_entries:
                        session.add(FactSalesInConstrained(
                            date_id=date_id_t,
                            cliente_id=cid,
                            sku_id=sku_id,
                            unidades_sales_in_constr=0.0,
                        ))
                        si_count += 1
                        if d_ct > 0.001:
                            lost_records.append((cid, date_id_t, d_ct))
                    # §3.5: inv_running unchanged, carries to next month

            else:
                # §3.5: Periods L+1..12: SI_constr = SI_unc (copy directly)
                for cid, d_ct in demand_by_month_client.get(date_id_t, []):
                    session.add(FactSalesInConstrained(
                        date_id=date_id_t,
                        cliente_id=cid,
                        sku_id=sku_id,
                        unidades_sales_in_constr=float(round(d_ct, 4)),
                    ))
                    si_count += 1

        # Insert per-client-period lost sales records
        for cid, did, lost_units in lost_records:
            session.add(RptLostSalesOos(
                sku_id=sku_id,
                cliente_id=cid,
                date_id=did,
                lost_sales_units=float(round(lost_units, 4)),
            ))
            lost_count += 1
            total_lost += lost_units

    session.flush()
    logger.info(
        "compute_sales_in_constrained: %d SI constrained records, "
        "%d SKUs with lost sales (total: %.0f units)",
        si_count, lost_count, total_lost,
    )
    return {
        "si_constrained_count": si_count,
        "lost_sales_skus": lost_count,
        "total_lost_sales": round(total_lost, 4),
    }


# ─────────────────────────────────────────────
# STEP 7: GATE — ALLOCATION REPORT GENERATION
# ─────────────────────────────────────────────


def generate_allocation_report(
    session: Session,
    mes_cierre_date_id: int,
) -> list[dict]:
    """
    For periods 1..L, query SI_unc and SI_constr, join with DimSku and DimCliente.

    Returns list of:
        {sku_id, upc, sku_descripcion, cliente_id, cliente_nombre,
         date_id, si_unc, asignacion, lost_sales}
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)
    sku_ids = _get_skus_with_demand(session, projection_ids)

    # Pre-load SI Unconstrained
    si_unc_rows = (
        session.query(
            FactSalesInUnconstrained.sku_id,
            FactSalesInUnconstrained.cliente_id,
            FactSalesInUnconstrained.date_id,
            FactSalesInUnconstrained.unidades_sales_in_unc,
        )
        .filter(FactSalesInUnconstrained.date_id.in_(projection_ids))
        .all()
    )
    si_unc_lookup: dict[tuple[int, int, int], float] = {}
    for r in si_unc_rows:
        si_unc_lookup[(r.sku_id, r.cliente_id, r.date_id)] = float(
            r.unidades_sales_in_unc or 0
        )

    # Pre-load SI Constrained
    si_constr_rows = (
        session.query(
            FactSalesInConstrained.sku_id,
            FactSalesInConstrained.cliente_id,
            FactSalesInConstrained.date_id,
            FactSalesInConstrained.unidades_sales_in_constr,
        )
        .filter(FactSalesInConstrained.date_id.in_(projection_ids))
        .all()
    )
    si_constr_lookup: dict[tuple[int, int, int], float] = {}
    for r in si_constr_rows:
        si_constr_lookup[(r.sku_id, r.cliente_id, r.date_id)] = float(
            r.unidades_sales_in_constr or 0
        )

    # Pre-load SKU info
    sku_info = {}
    for s in session.query(DimSku).filter(DimSku.sku_id.in_(sku_ids)).all():
        sku_info[s.sku_id] = {"upc": s.upc, "descripcion": s.sku_descripcion}

    # Pre-load client names
    client_names = {}
    for c in session.query(DimCliente).all():
        client_names[c.cliente_id] = c.cliente_nombre

    result: list[dict] = []

    for sku_id in sku_ids:
        L = _get_lead_time(session, sku_id)
        lt_ids = projection_ids[:L]
        info = sku_info.get(sku_id, {"upc": None, "descripcion": None})

        for date_id_t in lt_ids:
            clients = [
                (key[1], val)
                for key, val in si_unc_lookup.items()
                if key[0] == sku_id and key[2] == date_id_t
            ]

            for cliente_id, si_unc_val in clients:
                si_constr_val = si_constr_lookup.get(
                    (sku_id, cliente_id, date_id_t), 0.0
                )
                lost = round(si_unc_val - si_constr_val, 4)

                result.append({
                    "sku_id": sku_id,
                    "upc": int(info["upc"]) if info["upc"] else None,
                    "sku_descripcion": info["descripcion"],
                    "cliente_id": cliente_id,
                    "cliente_nombre": client_names.get(cliente_id, f"CLIENTE_{cliente_id}"),
                    "date_id": date_id_t,
                    "si_unc": round(si_unc_val, 4),
                    "asignacion": round(si_constr_val, 4),
                    "lost_sales": max(lost, 0.0),
                })

    logger.info(
        "generate_allocation_report: %d rows for %d SKUs", len(result), len(sku_ids)
    )
    return result


# ─────────────────────────────────────────────
# STEP 8: GATE — VALIDATION (§6)
# ─────────────────────────────────────────────


def validate_allocation_edits(
    session: Session,
    mes_cierre_date_id: int,
    edits: list[dict],
) -> dict:
    """
    Validation rule (§6): For each SKU and each period t (1..L):
        cumulative_assigned(SKU, t) = sum over k=1..t, all clients: Asignacion[SKU, client, period_k]
        cumulative_available(SKU, t) = inv_hist(SKU) + sum over k=1..t: Arrivals[SKU, period_k]
        cumulative_assigned(SKU, t) <= cumulative_available(SKU, t)

    Merges edits with existing FactSalesInConstrained values.

    Returns:
        {"valid": True} or {"valid": False, "errors": [...]}
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)

    # Load existing SI constrained
    si_constr_rows = (
        session.query(
            FactSalesInConstrained.sku_id,
            FactSalesInConstrained.cliente_id,
            FactSalesInConstrained.date_id,
            FactSalesInConstrained.unidades_sales_in_constr,
        )
        .filter(FactSalesInConstrained.date_id.in_(projection_ids))
        .all()
    )
    # Build lookup: {(sku_id, cliente_id, date_id): value}
    current_vals: dict[tuple[int, int, int], float] = {
        (r.sku_id, r.cliente_id, r.date_id): float(r.unidades_sales_in_constr or 0)
        for r in si_constr_rows
    }

    # Apply edits as overrides
    for edit in edits:
        key = (edit["sku_id"], edit["cliente_id"], edit["date_id"])
        current_vals[key] = float(edit["new_asignacion"])

    # Get all SKUs involved
    sku_ids = set(k[0] for k in current_vals.keys())

    errors: list[str] = []

    for sku_id in sorted(sku_ids):
        L = _get_lead_time(session, sku_id)
        lt_ids = projection_ids[:L]

        inv_hist = _get_internal_inventory_initial(session, sku_id, mes_cierre_date_id)
        arrivals = _get_transit_arrivals_by_month(session, sku_id, projection_ids)

        cum_assigned = 0.0
        cum_available = inv_hist

        for t, did in enumerate(lt_ids):
            # §6: cumulative arrivals
            cum_available += arrivals.get(did, 0.0)

            # §6: cumulative assigned (all clients for this SKU and period)
            period_assigned = sum(
                v for (sid, cid, d), v in current_vals.items()
                if sid == sku_id and d == did
            )
            cum_assigned += period_assigned

            if cum_assigned > cum_available + 0.01:  # rounding tolerance
                errors.append(
                    f"SKU {sku_id} period {t + 1} (date_id={did}): "
                    f"assigned {cum_assigned:.4f} > available {cum_available:.4f}"
                )

    if errors:
        return {"valid": False, "errors": errors}
    return {"valid": True}


# ─────────────────────────────────────────────
# STEP 9: GATE — APPLY APPROVED EDITS
# ─────────────────────────────────────────────


def apply_approved_allocation(
    session: Session,
    mes_cierre_date_id: int,
    edits: list[dict] | None = None,
) -> dict:
    """
    If edits is not None: update matching FactSalesInConstrained rows
    (match on sku_id + cliente_id + date_id). If no matching row exists, insert.

    Returns:
        {"estado": "APROBADO", "registros_modificados": int}
    """
    modified = 0

    if edits is not None:
        for edit in edits:
            row = (
                session.query(FactSalesInConstrained)
                .filter(
                    FactSalesInConstrained.sku_id == edit["sku_id"],
                    FactSalesInConstrained.cliente_id == edit["cliente_id"],
                    FactSalesInConstrained.date_id == edit["date_id"],
                )
                .first()
            )
            if row:
                row.unidades_sales_in_constr = float(round(edit["new_asignacion"], 4))
            else:
                session.add(FactSalesInConstrained(
                    date_id=edit["date_id"],
                    cliente_id=edit["cliente_id"],
                    sku_id=edit["sku_id"],
                    unidades_sales_in_constr=float(round(edit["new_asignacion"], 4)),
                ))
            modified += 1

        session.flush()

    # §6: Transition workflow state → APROBADO
    ctrl = (
        session.query(ConstraintProcessControl)
        .filter(ConstraintProcessControl.mes_cierre_date_id == mes_cierre_date_id)
        .first()
    )
    if ctrl:
        ctrl.estado = "APROBADO"
    else:
        logger.warning(
            "apply_approved_allocation: No ConstraintProcessControl found for %d. "
            "Creating one with estado=APROBADO.",
            mes_cierre_date_id,
        )
        session.add(ConstraintProcessControl(
            mes_cierre_date_id=mes_cierre_date_id,
            estado="APROBADO",
        ))
    session.flush()

    logger.info("apply_approved_allocation: %d records modified", modified)
    return {"estado": "APROBADO", "registros_modificados": modified}


# ─────────────────────────────────────────────
# STEP 6: ORCHESTRATOR (no commit inside)
# ─────────────────────────────────────────────


def run_constraint_process(
    session: Session,
    mes_cierre_date_id: int,
) -> dict:
    """
    Fase 3: Constraint Demand for a given close month.

    Pre-conditions:
    - Fase 2 (Unconstrained Demand) already executed.
    - Internal and transit inventory loaded.

    Steps:
    1. Theoretical inventory (in-memory).
    2. PO plan + final inventory.
    3. Persist constrained internal inventory.
    4. Sales In Constrained.

    NOTE: No session.commit() — only the API endpoint commits.

    Returns:
        dict with process summary.
    """
    start_time = datetime.now()
    summary = {
        "mes_cierre_date_id": mes_cierre_date_id,
        "fase": "CONSTRAINT_DEMAND",
        "estado": "EN_PROCESO",
        "pasos": {},
    }

    # Step 1: theoretical inventory (in-memory)
    logger.info("Constraint Step 1: Theoretical inventory...")
    inv_teo_all = compute_theoretical_inventory(session, mes_cierre_date_id)
    summary["pasos"]["1_inventario_teorico"] = {"skus": len(inv_teo_all)}

    # Step 2: PO plan + final inventory
    logger.info("Constraint Step 2: PO plan + final inventory...")
    po_count, inv_final_all = generate_po_plan(
        session, mes_cierre_date_id, inv_teo_all
    )
    summary["pasos"]["2_pos_generadas"] = {"pos": po_count}

    # Step 3: persist constrained internal inventory
    logger.info("Constraint Step 3: Persist constrained internal inventory...")
    inv_count = rebuild_internal_inventory_constrained(
        session, mes_cierre_date_id, inv_final_all
    )
    summary["pasos"]["3_inventario_constrained"] = {"registros": inv_count}

    # Step 4: Sales In Constrained
    logger.info("Constraint Step 4: Sales In Constrained...")
    constr_result = compute_sales_in_constrained(session, mes_cierre_date_id)
    summary["pasos"]["4_sales_in_constrained"] = constr_result

    session.flush()  # §: no commit

    # §6: Persist workflow state — COMPLETADO_PENDIENTE_APROBACION
    ctrl = (
        session.query(ConstraintProcessControl)
        .filter(ConstraintProcessControl.mes_cierre_date_id == mes_cierre_date_id)
        .first()
    )
    if ctrl:
        ctrl.estado = "COMPLETADO_PENDIENTE_APROBACION"
        ctrl.detalles = json.dumps({
            "po_count": po_count,
            "lost_sales_skus": constr_result["lost_sales_skus"],
            "total_lost_sales": constr_result["total_lost_sales"],
        })
    else:
        session.add(ConstraintProcessControl(
            mes_cierre_date_id=mes_cierre_date_id,
            estado="COMPLETADO_PENDIENTE_APROBACION",
            detalles=json.dumps({
                "po_count": po_count,
                "lost_sales_skus": constr_result["lost_sales_skus"],
                "total_lost_sales": constr_result["total_lost_sales"],
            }),
        ))
    session.flush()

    summary["estado"] = "COMPLETADO_PENDIENTE_APROBACION"
    summary["mensaje"] = (
        f"Phase A done. Gate approval required before Phase B. "
        f"{po_count} POs, {constr_result['lost_sales_skus']} SKUs with lost sales "
        f"(total: {constr_result['total_lost_sales']:,.0f} units)."
    )
    summary["duracion_segundos"] = (datetime.now() - start_time).total_seconds()

    logger.info(
        "run_constraint_process completed for month %d", mes_cierre_date_id
    )
    return summary
