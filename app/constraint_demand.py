"""
constraint_demand.py — Fase 3: Constraint Demand.

Calcula, para cada SKU y horizonte de 12 meses de proyeccion:
    1. Inventario interno teorico 1..12 (permitiendo negativos).
    2. Plan de ordenes de compra PO1..PO5 con lead time 4, donde los
       periodos 1..4 no son afectables por POs (solo inv inicial + transito).
       Unico truncamiento permitido: inv_teo de periodos 1..4, despues de PO1.
    3. Inventario interno definitivo 1..12 con POs incluidas.
       Si alguno resulta negativo → error de proceso (no se trunca a 0).
    4. Sales In Constrained (limitadas por stock interno en ventana 1..L).
    5. Lost Sales por OOS irrecuperable en ventana de lead time.

Invariantes:
    - Todos los calculos de inventario interno son a nivel SKU global (no por cliente).
    - Fase 3 no recalcula ni toca Sales Out.
    - Lead time logico: 4 periodos (orden en periodo n → llega en periodo n+4).
    - La politica de inventario TENKA: cobertura de demanda por ventana de 4 periodos.
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
from .monthly_close import _compute_month_minus_n, _compute_month_plus_n

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# HELPERS (firmas publicas sin cambios)
# ─────────────────────────────────────────────


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


class ConstraintProcessError(Exception):
    """Error de proceso en Constraint Demand (inventario negativo inesperado)."""
    pass


# ─────────────────────────────────────────────
# PASO 1: INVENTARIO INTERNO TEORICO 1..12 (PERMITIENDO NEGATIVOS)
# ─────────────────────────────────────────────


def simulate_internal_inventory_window(
    session: Session,
    mes_cierre_date_id: int,
) -> int:
    """
    Calcula el inventario interno teorico para cada SKU en los 12 periodos
    de proyeccion, permitiendo inventario negativo.

    Formula:
        Periodo 1: inv_teo[1] = inv_hist_final - SI_unc[1] + arrivals[1]
        Periodo t: inv_teo[t] = inv_teo[t-1] - SI_unc[t] + arrivals[t]

    Los valores negativos se permiten en esta etapa. No se trunca nada.
    Los resultados NO se insertan en la BD aqui (se usan internamente
    por generate_po_plan). Se devuelve la estructura en memoria para
    el paso 2.

    Returns:
        Numero de SKUs procesados (para compatibilidad de logging).
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)
    sku_ids = _get_skus_with_demand(session, projection_ids)

    if not sku_ids:
        logger.warning("simulate_internal_inventory_window: No hay SKUs con demanda.")
        return 0

    # Nota: Ya no insertamos en fact_inventory_internal_constrained aqui.
    # Solo calculamos y logueamos. El paso 3 (rebuild) hara la insercion final.
    count = 0
    for sku_id in sku_ids:
        inv_int_0 = _get_internal_inventory_initial(session, sku_id, mes_cierre_date_id)
        demand = _get_demand_by_month(session, sku_id, projection_ids)
        arrivals = _get_transit_arrivals_by_month(session, sku_id, projection_ids)

        inv_prev = inv_int_0
        for t in range(len(projection_ids)):
            date_id_t = projection_ids[t]
            demand_t = demand.get(date_id_t, 0.0)
            arrivals_t = arrivals.get(date_id_t, 0.0)
            inv_t = inv_prev - demand_t + arrivals_t
            inv_prev = inv_t

        count += 1

    logger.info(
        "simulate_internal_inventory_window: %d SKUs procesados (inventario teorico 1..12)",
        count,
    )
    return count


# ─────────────────────────────────────────────
# PASO 2: GENERACION DE POs (PO1..PO5 SECUENCIALES, LT=4)
# ─────────────────────────────────────────────


def generate_po_plan(
    session: Session,
    mes_cierre_date_id: int,
) -> int:
    """
    Genera POs internas PO1..PO5 por SKU segun la logica secuencial de
    lead time 4 periodos.

    Logica:
    1. Construir inventario teorico 1..12 (sin POs, permitiendo negativos).
    2. PO1 (orden periodo 1, llega periodo 5):
       - R1 = inv_teo[4] - (SI_unc[5]+SI_unc[6]+SI_unc[7]+SI_unc[8]) + arrivals[5]
       - PO1 = abs(R1) si R1 < 0, sino 0.
    3. Truncamiento unico: inv_teo[1..4] → max(0, inv_teo[t]) para t=1..4.
    4. Inventario real periodo 5:
       inv_5 = inv_teo_truncado[4] - SI_unc[5] + arrivals[5] + PO1
    5. PO2..PO5: repetir patron secuencialmente (base = inv anterior real).

    Inserta en fact_po_interno.

    Returns:
        Numero de POs insertadas.
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)
    sku_ids = _get_skus_with_demand(session, projection_ids)
    n_proj = len(projection_ids)  # siempre 12

    # Limpiar POs previas
    session.query(FactPoInterno).delete(synchronize_session="fetch")

    po_count = 0

    for sku_id in sku_ids:
        L = _get_lead_time(session, sku_id)
        inv_int_0 = _get_internal_inventory_initial(session, sku_id, mes_cierre_date_id)
        demand = _get_demand_by_month(session, sku_id, projection_ids)
        arrivals = _get_transit_arrivals_by_month(session, sku_id, projection_ids)

        # Helper: SI_unc para periodo t (0-indexed)
        def si_unc(t: int) -> float:
            if 0 <= t < n_proj:
                return demand.get(projection_ids[t], 0.0)
            return 0.0

        # Helper: llegadas de transito para periodo t (0-indexed)
        def arr(t: int) -> float:
            if 0 <= t < n_proj:
                return arrivals.get(projection_ids[t], 0.0)
            return 0.0

        # ── 1. Construir inventario teorico 1..12 sin POs, permitiendo negativos ──
        inv_teo = [0.0] * n_proj
        inv_prev = inv_int_0
        for t in range(n_proj):
            inv_teo[t] = inv_prev - si_unc(t) + arr(t)
            inv_prev = inv_teo[t]

        # ── 2. PO1 (orden periodo 1 [idx 0], llega periodo 5 [idx 4]) ──
        # R1 = inv_teo[4] - (SI_unc[5]+SI_unc[6]+SI_unc[7]+SI_unc[8]) + arrivals[5]
        # Nota: indices 0-based. Periodo 4 = idx 3, periodo 5 = idx 4, etc.
        # Pero el spec dice "PO del periodo 1" = primer periodo de proyeccion.
        # Con L=4: PO1 orden en periodo 1 (idx 0), llega periodo 5 (idx 4).
        # R1 usa inv_teo de periodo 4 (idx 3), demanda periodos 5..8 (idx 4..7),
        # arrivals periodo 5 (idx 4).

        if L > n_proj:
            # Lead time mayor que horizonte; no podemos generar POs utiles
            continue

        # Numero maximo de POs que podemos generar: periodos 1..max donde llegada <= 12
        # PO_n: orden periodo n (idx n-1), llega periodo n+L (idx n+L-1)
        # La ultima PO posible: n+L-1 < 12 → n < 12-L+1 = 9 (para L=4 → PO1..PO5 max)
        max_pos = min(n_proj - L, n_proj)  # periodos que pueden tener PO

        # Track inventarios reales post-PO para periodos >= L+1
        # Primero procesamos PO1
        po_units_list: list[float] = []  # PO1, PO2, ...

        # ── PO1 ──
        # inv_teo al final de periodo L (idx L-1)
        inv_teo_at_L = inv_teo[L - 1] if L - 1 < n_proj else 0.0

        # Demanda de periodos L+1 .. 2L (idx L .. 2L-1)
        S1 = sum(si_unc(t) for t in range(L, min(2 * L, n_proj)))

        # Llegadas de transito en periodo L+1 (idx L)
        arrivals_at_arrival = arr(L)

        R1 = inv_teo_at_L - S1 + arrivals_at_arrival
        PO1 = abs(R1) if R1 < 0 else 0.0
        po_units_list.append(PO1)

        # Insertar PO1 si > 0
        if PO1 > 0.5 and L < n_proj:
            session.add(FactPoInterno(
                sku_id=sku_id,
                date_id_orden=projection_ids[0],
                date_id_llegada=projection_ids[L],
                unidades_po=float(round(PO1, 4)),
            ))
            po_count += 1

        # ── 3. Truncamiento unico: inv_teo[1..L] → max(0, val) ──
        for t in range(L):
            if inv_teo[t] < 0:
                inv_teo[t] = 0.0

        # ── 4. Inventario real del periodo L+1 (idx L) ──
        # inv_real[L] = inv_teo_truncado[L-1] - SI_unc[L+1] + arrivals[L+1] + PO1
        # (idx: inv_teo[L-1] - si_unc(L) + arr(L) + PO1)
        if L < n_proj:
            inv_real_current = inv_teo[L - 1] - si_unc(L) + arr(L) + PO1
        else:
            inv_real_current = 0.0

        # ── 5. PO2..POmax secuencialmente ──
        for po_num in range(2, max_pos + 1):
            # PO_n: orden periodo n (idx n-1), llega periodo n+L (idx n+L-1)
            order_idx = po_num - 1  # 0-based index del periodo de orden
            arrival_idx = po_num - 1 + L  # 0-based index del periodo de llegada

            if arrival_idx >= n_proj:
                break

            # base = inventario real al final del periodo de llegada - 1
            # (que es inv_real_current del periodo anterior de llegada)
            base = inv_real_current

            # Demanda de L periodos siguientes al periodo de llegada
            # PO_n llega en periodo n+L (idx arrival_idx)
            # Demanda = SI_unc de periodos (arrival_idx+1)..(arrival_idx+L)
            S_n = sum(si_unc(t) for t in range(arrival_idx + 1, min(arrival_idx + L + 1, n_proj)))

            # Llegadas de transito en periodo de llegada
            arr_n = arr(arrival_idx)

            R_n = base - S_n + arr_n
            PO_n = abs(R_n) if R_n < 0 else 0.0
            po_units_list.append(PO_n)

            # Insertar PO_n si > 0
            if PO_n > 0.5:
                session.add(FactPoInterno(
                    sku_id=sku_id,
                    date_id_orden=projection_ids[order_idx],
                    date_id_llegada=projection_ids[arrival_idx],
                    unidades_po=float(round(PO_n, 4)),
                ))
                po_count += 1

            # Calcular inventario real del periodo de llegada
            # inv_real = base - SI_unc[arrival] + arrivals[arrival] + PO_n
            inv_real_current = base - si_unc(arrival_idx) + arr(arrival_idx) + PO_n

    session.flush()
    logger.info("generate_po_plan: %d POs generadas para %d SKUs", po_count, len(sku_ids))
    return po_count


# ─────────────────────────────────────────────
# PASO 3: INVENTARIO INTERNO FINAL CONSTRAINT 1..12
# ─────────────────────────────────────────────


def rebuild_internal_inventory_constrained(
    session: Session,
    mes_cierre_date_id: int,
) -> dict:
    """
    Calcula inventario interno final 1..12 y guarda en
    FactInventoryInternalConstrained.

    Reglas:
        Periodo 1: inv_1 = inv_hist_final - SI_unc[1] + arrivals_1
        Periodos 2..4: inv_t = inv_{t-1} - SI_unc[t] + arrivals_t
        Periodos 5..12: inv_t = inv_{t-1} - SI_unc[t] + arrivals_t + POs_que_llegan_en_t

    NO se truncan inventarios a 0. Si inv_t < 0 para cualquier t,
    se considera error de proceso.

    Returns:
        dict con 'inserted' (int) y 'negative_inventory_errors' (list).
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)
    sku_ids = _get_skus_with_demand(session, projection_ids)
    L = 4  # Lead time logico del negocio

    # Limpiar datos previos
    session.query(FactInventoryInternalConstrained).filter(
        FactInventoryInternalConstrained.date_id.in_(projection_ids),
    ).delete(synchronize_session="fetch")

    # Pre-cargar PO arrivals por (sku_id, date_id_llegada)
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
    negative_errors: list[dict] = []

    for sku_id in sku_ids:
        sku_L = _get_lead_time(session, sku_id)
        inv_int_0 = _get_internal_inventory_initial(session, sku_id, mes_cierre_date_id)
        demand = _get_demand_by_month(session, sku_id, projection_ids)
        arrivals = _get_transit_arrivals_by_month(session, sku_id, projection_ids)

        inv_prev = inv_int_0

        for t in range(len(projection_ids)):
            date_id_t = projection_ids[t]
            demand_t = demand.get(date_id_t, 0.0)
            arrivals_t = arrivals.get(date_id_t, 0.0)

            # POs solo llegan en periodos L+1..12 (idx >= L)
            if t >= sku_L:
                arrivals_po_t = po_by_sku_month.get((sku_id, date_id_t), 0.0)
            else:
                arrivals_po_t = 0.0

            inv_t = inv_prev - demand_t + arrivals_t + arrivals_po_t

            # NO truncar a 0. Si es negativo, registrar error.
            if inv_t < -0.5:  # tolerancia de redondeo
                negative_errors.append({
                    "sku_id": sku_id,
                    "date_id": date_id_t,
                    "periodo": t + 1,
                    "inventario": round(inv_t, 4),
                })
                logger.error(
                    "Inventario interno negativo: SKU %d, periodo %d (date_id=%d), inv=%.2f",
                    sku_id, t + 1, date_id_t, inv_t,
                )

            session.add(FactInventoryInternalConstrained(
                date_id=date_id_t,
                sku_id=sku_id,
                inventario_final_int=float(round(inv_t, 4)),
            ))
            inserted += 1
            inv_prev = inv_t

    session.flush()
    logger.info(
        "rebuild_internal_inventory_constrained: %d registros para %d SKUs, %d errores de inv negativo",
        inserted, len(sku_ids), len(negative_errors),
    )
    return {
        "inserted": inserted,
        "negative_inventory_errors": negative_errors,
    }


# ─────────────────────────────────────────────
# PASO 4: SALES IN CONSTRAINED + LOST SALES
# ─────────────────────────────────────────────


def compute_sales_in_constrained(
    session: Session,
    mes_cierre_date_id: int,
) -> dict:
    """
    Calcula Sales In Constrained para todos los meses de proyeccion:

    Para cada SKU:
      - L = lead_time_meses de DimSku (default 4).
      - Periodos 1..L:
          * inv_running se inicia en inventario interno inicial + llegadas
            de transito en cada mes.
          * Si inv_running >= demanda_total: SI_constr = SI_unc (sin cambios).
          * Si inv_running > 0 pero < demanda_total: reparto proporcional,
            inv_running = 0, oos_triggered = True.
          * Una vez oos_triggered = True: SI_constr = 0 para todos
            los meses restantes de 1..L (lost sales).
      - Periodos L+1..12: SI_constr = SI_unc (sin cambios).

    Tambien llena RptLostSalesOos con lost_sales_total por SKU y detalle JSON.

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
    1) Calcular inventario interno teorico 1..12 (permitiendo negativos).
    2) Generar PO1..PO5 secuenciales (fact_po_interno).
       Unico truncamiento: inv_teo periodos 1..4, despues de PO1.
    3) Calcular inventario interno definitivo 1..12 con POs
       (fact_inventory_internal_constrained). Si alguno < 0 → error.
    4) Calcular Sales In Constrained 1..L + lost sales por OOS.
    5) Igualar Sales In Constrained = Unconstrained en L+1..12
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
        # Paso 1: Inventario interno teorico 1..12
        logger.info("Constraint Paso 1: Calculando inventario interno teorico 1..12...")
        sim_count = simulate_internal_inventory_window(session, mes_cierre_date_id)
        summary["pasos"]["1_inventario_teorico"] = {"skus_procesados": sim_count}

        # Paso 2: Generar POs secuenciales (PO1..PO5)
        logger.info("Constraint Paso 2: Generando plan de POs (PO1..PO5 secuenciales)...")
        po_count = generate_po_plan(session, mes_cierre_date_id)
        summary["pasos"]["2_pos_generadas"] = {"pos": po_count}

        # Paso 3: Inventario interno definitivo 1..12 con POs
        logger.info("Constraint Paso 3: Calculando inventario interno definitivo con POs...")
        inv_result = rebuild_internal_inventory_constrained(session, mes_cierre_date_id)
        summary["pasos"]["3_inventario_definitivo"] = {
            "registros": inv_result["inserted"],
            "errores_negativos": len(inv_result["negative_inventory_errors"]),
        }

        # Si hay errores de inventario negativo, marcar como ERROR
        if inv_result["negative_inventory_errors"]:
            neg_count = len(inv_result["negative_inventory_errors"])
            logger.error(
                "Constraint: %d inventarios internos negativos detectados. "
                "Esto indica un error en el calculo de POs.",
                neg_count,
            )
            summary["errores_inventario_negativo"] = inv_result["negative_inventory_errors"]
            summary["estado"] = "ERROR"
            summary["mensaje"] = (
                f"Constraint Demand con errores: {neg_count} inventarios internos "
                f"negativos detectados tras incluir POs. "
                f"Revisar calculo de POs para los SKUs afectados."
            )
            summary["duracion_segundos"] = (datetime.now() - start_time).total_seconds()
            session.flush()
            logger.warning(
                "run_constraint_process: continuando con Sales In Constrained "
                "a pesar de errores de inventario negativo."
            )
            # Nota: continuamos para completar Sales In Constrained y Lost Sales,
            # pero el estado queda como ERROR.

        # Paso 4: Sales In Constrained + Lost Sales
        logger.info("Constraint Paso 4: Calculando Sales In Constrained y Lost Sales...")
        constr_result = compute_sales_in_constrained(session, mes_cierre_date_id)
        summary["pasos"]["4_sales_in_constrained"] = constr_result

        if summary["estado"] != "ERROR":
            summary["estado"] = "COMPLETADO"
            summary["mensaje"] = (
                f"Constraint Demand completado. "
                f"{po_count} POs generadas, "
                f"{constr_result['lost_sales_skus']} SKUs con lost sales "
                f"(total: {constr_result['total_lost_sales']:,.0f} unidades)."
            )
        else:
            # Agregar info de POs y lost sales al mensaje de error existente
            summary["mensaje"] += (
                f" POs generadas: {po_count}. "
                f"Lost sales: {constr_result['lost_sales_skus']} SKUs "
                f"({constr_result['total_lost_sales']:,.0f} unidades)."
            )

        summary["duracion_segundos"] = (datetime.now() - start_time).total_seconds()

        # FIX BUG-05: No hacer commit aqui; dejar que el caller (api.py)
        # controle la boundary transaccional. Solo flush para que los datos
        # sean visibles dentro de la misma sesion.
        session.flush()
        logger.info("run_constraint_process completado para mes %d", mes_cierre_date_id)
        return summary

    except Exception as e:
        session.rollback()
        summary["estado"] = "ERROR"
        summary["mensaje"] = f"Error en Constraint Demand: {str(e)}"
        summary["duracion_segundos"] = (datetime.now() - start_time).total_seconds()
        logger.exception("Error en run_constraint_process para mes %d", mes_cierre_date_id)
        raise
