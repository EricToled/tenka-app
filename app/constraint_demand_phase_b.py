"""
constraint_demand_phase_b.py — Fases B y C: Sales Out Constraint, Inventario
Cliente Constraint, Reportes de Ventas Perdidas, DOS Constraint.

Fase B:
    1. Sales Out Constrained por (cliente, sku, periodo) 1..12.
    2. Inventario final del cliente Constrained por (cliente, sku, periodo) 1..12.

Fase C:
    3. Reporte de ventas perdidas Sales Out (periodos 1..L).
    4. Reporte de ventas perdidas Sales In (periodos 1..L).
    5. DOS Constraint del cliente (por cliente-SKU-periodo).
    6. DOS Constraint interno (por SKU-periodo).

Pre-condiciones:
    - Fase A completada (run_constraint_process).
    - Reporte de asignacion aprobado por el usuario.
    - fact_sales_in_constrained actualizado con valores aprobados.

Reglas clave:
    - SO_constr = SO_unc si inv_tentativo >= 0.
    - SO_constr = SO_unc - (SI_unc - SI_constr) si inv_tentativo < 0.
    - Si aun negativo: SO_constr = inv_prev + SI_constr (vender solo lo disponible).
    - DOS usa ventana N-ajustada con first_sale_date (misma formula que historico).
"""

import logging
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from .constraint_demand import (
    _compute_month_plus_n,
    _get_lead_time,
    _get_projection_ids,
    _get_skus_with_demand,
)
from .eligibility import _generate_date_id_range
from .models import (
    DimSku,
    FactDosConstraintCliente,
    FactDosConstraintInterno,
    FactInventoryClientConstrained,
    FactInventoryInternalConstrained,
    FactSalesIn,
    FactSalesInConstrained,
    FactSalesInUnconstrained,
    FactSalesOut,
    FactSalesOutConstrained,
    FactSalesOutUnconstrained,
    FactStockCliente,
    RptLostSalesSalesIn,
    RptLostSalesSalesOut,
)
from .monthly_close import _compute_month_minus_n

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# FASE B: SALES OUT CONSTRAINED + INVENTARIO CLIENTE CONSTRAINED
# ─────────────────────────────────────────────


def compute_sales_out_constrained(
    session: Session,
    mes_cierre_date_id: int,
) -> dict:
    """
    Calcula Sales Out Constrained e Inventario final del cliente Constrained
    para los 12 periodos de proyeccion.

    Algoritmo para cada par (cliente_id, sku_id) y periodo t = 1..12:

        inv_tentativo = inv_prev + SI_constr[t] - SO_unc[t]

        Si inv_tentativo >= 0:
            SO_constr[t] = SO_unc[t]
            inv_final[t] = inv_tentativo

        Si inv_tentativo < 0:
            SO_constr[t] = SO_unc[t] - (SI_unc[t] - SI_constr[t])
            inv_final[t] = inv_prev + SI_constr[t] - SO_constr[t]

            Si inv_final[t] < 0 (aun despues del ajuste):
                SO_constr[t] = inv_prev + SI_constr[t]  # vender solo lo disponible
                inv_final[t] = 0

    Args:
        session: Sesion de SQLAlchemy activa.
        mes_cierre_date_id: date_id del ultimo mes historico (YYYYMM).

    Returns:
        dict con so_constrained_count, inv_client_constrained_count, adjustments.
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)

    # Obtener todos los pares (cliente_id, sku_id) con SI constrained
    pairs_rows = (
        session.query(
            FactSalesInConstrained.cliente_id,
            FactSalesInConstrained.sku_id,
        )
        .filter(FactSalesInConstrained.date_id.in_(projection_ids))
        .distinct()
        .all()
    )
    pairs = [(r.cliente_id, r.sku_id) for r in pairs_rows]

    # Limpiar datos previos
    session.query(FactSalesOutConstrained).filter(
        FactSalesOutConstrained.date_id.in_(projection_ids),
    ).delete(synchronize_session="fetch")
    session.query(FactInventoryClientConstrained).filter(
        FactInventoryClientConstrained.date_id.in_(projection_ids),
    ).delete(synchronize_session="fetch")

    # Pre-cargar datos en memoria para eficiencia
    # SI Constrained
    si_constr_rows = (
        session.query(
            FactSalesInConstrained.cliente_id,
            FactSalesInConstrained.sku_id,
            FactSalesInConstrained.date_id,
            FactSalesInConstrained.unidades_sales_in_constr,
        )
        .filter(FactSalesInConstrained.date_id.in_(projection_ids))
        .all()
    )
    si_constr: dict[tuple[int, int, int], float] = {
        (r.cliente_id, r.sku_id, r.date_id): float(r.unidades_sales_in_constr or 0)
        for r in si_constr_rows
    }

    # SI Unconstrained
    si_unc_rows = (
        session.query(
            FactSalesInUnconstrained.cliente_id,
            FactSalesInUnconstrained.sku_id,
            FactSalesInUnconstrained.date_id,
            FactSalesInUnconstrained.unidades_sales_in_unc,
        )
        .filter(FactSalesInUnconstrained.date_id.in_(projection_ids))
        .all()
    )
    si_unc: dict[tuple[int, int, int], float] = {
        (r.cliente_id, r.sku_id, r.date_id): float(r.unidades_sales_in_unc or 0)
        for r in si_unc_rows
    }

    # SO Unconstrained
    so_unc_rows = (
        session.query(
            FactSalesOutUnconstrained.cliente_id,
            FactSalesOutUnconstrained.sku_id,
            FactSalesOutUnconstrained.date_id,
            FactSalesOutUnconstrained.unidades_sales_out_unc,
        )
        .filter(FactSalesOutUnconstrained.date_id.in_(projection_ids))
        .all()
    )
    so_unc: dict[tuple[int, int, int], float] = {
        (r.cliente_id, r.sku_id, r.date_id): float(r.unidades_sales_out_unc or 0)
        for r in so_unc_rows
    }

    # Inventario historico del cliente: {(cliente_id, sku_id): inv_final}
    inv_hist_rows = (
        session.query(
            FactStockCliente.cliente_id,
            FactStockCliente.sku_id,
            func.sum(FactStockCliente.inventario_final_unidades).label("total"),
        )
        .filter(FactStockCliente.date_id == mes_cierre_date_id)
        .group_by(FactStockCliente.cliente_id, FactStockCliente.sku_id)
        .all()
    )
    inv_hist: dict[tuple[int, int], float] = {
        (r.cliente_id, r.sku_id): float(r.total or 0) for r in inv_hist_rows
    }

    so_count = 0
    inv_count = 0
    adjustments = 0

    for cliente_id, sku_id in pairs:
        inv_prev = inv_hist.get((cliente_id, sku_id), 0.0)

        for t in range(len(projection_ids)):
            date_id_t = projection_ids[t]
            key = (cliente_id, sku_id, date_id_t)

            si_constr_t = si_constr.get(key, 0.0)
            si_unc_t = si_unc.get(key, 0.0)
            so_unc_t = so_unc.get(key, 0.0)

            # Paso 1: Inventario tentativo
            inv_tentativo = inv_prev + si_constr_t - so_unc_t

            if inv_tentativo >= 0:
                # Sin ajuste necesario
                so_constr_t = so_unc_t
                inv_final_t = inv_tentativo
            else:
                # Ajustar SO para evitar inventario negativo
                so_constr_t = so_unc_t - (si_unc_t - si_constr_t)
                inv_final_t = inv_prev + si_constr_t - so_constr_t

                if inv_final_t < -0.5:
                    # Forzar: vender solo lo disponible
                    so_constr_t = inv_prev + si_constr_t
                    inv_final_t = 0.0
                    logger.warning(
                        "SO Constrained forzado a %.2f para cliente=%d, sku=%d, "
                        "periodo=%d (date_id=%d). inv_prev=%.2f, SI_constr=%.2f, "
                        "SO_unc=%.2f",
                        so_constr_t,
                        cliente_id,
                        sku_id,
                        t + 1,
                        date_id_t,
                        inv_prev,
                        si_constr_t,
                        so_unc_t,
                    )
                elif inv_final_t < 0:
                    inv_final_t = 0.0  # tolerancia de redondeo

                adjustments += 1

            # Asegurar que SO_constr no sea negativo
            if so_constr_t < 0:
                so_constr_t = 0.0

            session.add(
                FactSalesOutConstrained(
                    date_id=date_id_t,
                    cliente_id=cliente_id,
                    sku_id=sku_id,
                    unidades_sales_out_constr=float(round(so_constr_t, 4)),
                )
            )
            so_count += 1

            session.add(
                FactInventoryClientConstrained(
                    date_id=date_id_t,
                    cliente_id=cliente_id,
                    sku_id=sku_id,
                    inventario_final_constr=float(round(inv_final_t, 4)),
                )
            )
            inv_count += 1

            inv_prev = inv_final_t

    session.flush()
    logger.info(
        "compute_sales_out_constrained: %d SO_constr, %d inv_client, "
        "%d ajustes realizados, %d pares (cliente, sku)",
        so_count,
        inv_count,
        adjustments,
        len(pairs),
    )
    return {
        "so_constrained_count": so_count,
        "inv_client_constrained_count": inv_count,
        "adjustments": adjustments,
    }


# ─────────────────────────────────────────────
# FASE C.1: REPORTE VENTAS PERDIDAS SALES OUT
# ─────────────────────────────────────────────


def generate_lost_sales_report_so(
    session: Session,
    mes_cierre_date_id: int,
) -> int:
    """
    Genera el reporte de ventas perdidas Sales Out para periodos 1..L.

    Lost = SO_unc - SO_constr. Solo inserta si lost > 0.

    Args:
        session: Sesion de SQLAlchemy activa.
        mes_cierre_date_id: date_id del ultimo mes historico (YYYYMM).

    Returns:
        Numero de registros insertados.
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)
    sku_ids = _get_skus_with_demand(session, projection_ids)

    # Limpiar datos previos
    session.query(RptLostSalesSalesOut).delete(synchronize_session="fetch")

    inserted = 0

    for sku_id in sku_ids:
        L = _get_lead_time(session, sku_id)
        lt_ids = projection_ids[:L]

        for t, date_id_t in enumerate(lt_ids):
            # Obtener SO_unc y SO_constr para todos los clientes
            so_unc_rows = (
                session.query(
                    FactSalesOutUnconstrained.cliente_id,
                    FactSalesOutUnconstrained.unidades_sales_out_unc,
                )
                .filter(
                    FactSalesOutUnconstrained.sku_id == sku_id,
                    FactSalesOutUnconstrained.date_id == date_id_t,
                )
                .all()
            )

            for r in so_unc_rows:
                so_unc_val = float(r.unidades_sales_out_unc or 0)

                so_constr_row = (
                    session.query(FactSalesOutConstrained.unidades_sales_out_constr)
                    .filter(
                        FactSalesOutConstrained.sku_id == sku_id,
                        FactSalesOutConstrained.cliente_id == r.cliente_id,
                        FactSalesOutConstrained.date_id == date_id_t,
                    )
                    .scalar()
                )
                so_constr_val = float(so_constr_row or 0)

                lost = so_unc_val - so_constr_val
                if lost > 0.005:  # tolerancia de redondeo
                    session.add(
                        RptLostSalesSalesOut(
                            cliente_id=r.cliente_id,
                            sku_id=sku_id,
                            date_id=date_id_t,
                            periodo_proyeccion=t + 1,
                            so_unconstrained=float(round(so_unc_val, 4)),
                            so_constrained=float(round(so_constr_val, 4)),
                            lost_sales=float(round(lost, 4)),
                        )
                    )
                    inserted += 1

    session.flush()
    logger.info(
        "generate_lost_sales_report_so: %d registros de ventas perdidas SO",
        inserted,
    )
    return inserted


# ─────────────────────────────────────────────
# FASE C.2: REPORTE VENTAS PERDIDAS SALES IN
# ─────────────────────────────────────────────


def generate_lost_sales_report_si(
    session: Session,
    mes_cierre_date_id: int,
) -> int:
    """
    Genera el reporte de ventas perdidas Sales In para periodos 1..L.

    Lost = SI_unc - SI_constr. Solo inserta si lost > 0.

    Args:
        session: Sesion de SQLAlchemy activa.
        mes_cierre_date_id: date_id del ultimo mes historico (YYYYMM).

    Returns:
        Numero de registros insertados.
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)
    sku_ids = _get_skus_with_demand(session, projection_ids)

    # Limpiar datos previos
    session.query(RptLostSalesSalesIn).delete(synchronize_session="fetch")

    inserted = 0

    for sku_id in sku_ids:
        L = _get_lead_time(session, sku_id)
        lt_ids = projection_ids[:L]

        for t, date_id_t in enumerate(lt_ids):
            si_unc_rows = (
                session.query(
                    FactSalesInUnconstrained.cliente_id,
                    FactSalesInUnconstrained.unidades_sales_in_unc,
                )
                .filter(
                    FactSalesInUnconstrained.sku_id == sku_id,
                    FactSalesInUnconstrained.date_id == date_id_t,
                )
                .all()
            )

            for r in si_unc_rows:
                si_unc_val = float(r.unidades_sales_in_unc or 0)

                si_constr_row = (
                    session.query(FactSalesInConstrained.unidades_sales_in_constr)
                    .filter(
                        FactSalesInConstrained.sku_id == sku_id,
                        FactSalesInConstrained.cliente_id == r.cliente_id,
                        FactSalesInConstrained.date_id == date_id_t,
                    )
                    .scalar()
                )
                si_constr_val = float(si_constr_row or 0)

                lost = si_unc_val - si_constr_val
                if lost > 0.005:  # tolerancia de redondeo
                    session.add(
                        RptLostSalesSalesIn(
                            cliente_id=r.cliente_id,
                            sku_id=sku_id,
                            date_id=date_id_t,
                            periodo_proyeccion=t + 1,
                            si_unconstrained=float(round(si_unc_val, 4)),
                            si_constrained=float(round(si_constr_val, 4)),
                            lost_sales=float(round(lost, 4)),
                        )
                    )
                    inserted += 1

    session.flush()
    logger.info(
        "generate_lost_sales_report_si: %d registros de ventas perdidas SI",
        inserted,
    )
    return inserted


# ─────────────────────────────────────────────
# FASE C.3: DOS CONSTRAINT (CLIENTE + INTERNO)
# ─────────────────────────────────────────────


def compute_dos_constraint(
    session: Session,
    mes_cierre_date_id: int,
) -> dict:
    """
    Calcula DOS Constraint para cliente (fact_dos_constraint_cliente) e
    interno (fact_dos_constraint_interno) para los 12 periodos de proyeccion.

    Formula (misma que DOS historico, con datos Constraint):

    DOS Cliente:
        inv_final = fact_inventory_client_constrained[t]
        promedio = SUM(SO trailing 12m) / N
        mos = inv_final / promedio
        dos = mos * 30

    DOS Interno:
        inv_final = fact_inventory_internal_constrained[t]
        promedio = SUM(SI trailing 12m, agregado todos clientes) / N
        mos = inv_final / promedio
        dos = mos * 30

    Donde N = min(12, meses desde first_sale_date). Si promedio = 0, DOS = NULL.

    La ventana trailing de 12 meses mezcla datos historicos (fact_sales_out /
    fact_sales_in) con datos proyectados (fact_sales_out_constrained /
    fact_sales_in_constrained).

    Args:
        session: Sesion de SQLAlchemy activa.
        mes_cierre_date_id: date_id del ultimo mes historico (YYYYMM).

    Returns:
        dict con dos_cliente_count y dos_interno_count.
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)

    # Ventana historica: 12 meses hasta mes_cierre (inclusive)
    hist_start = _compute_month_minus_n(mes_cierre_date_id, 11)
    hist_ids = _generate_date_id_range(hist_start, mes_cierre_date_id)

    # Todos los date_ids combinados (historico + proyectado) = 24 meses
    all_ids = hist_ids + projection_ids

    # ── Pre-cargar first_sale_date por (cliente_id, sku_id) desde SO historico ──
    first_sale_rows = (
        session.query(
            FactSalesOut.cliente_id,
            FactSalesOut.sku_id,
            func.min(FactSalesOut.date_id).label("first_date"),
        )
        .group_by(FactSalesOut.cliente_id, FactSalesOut.sku_id)
        .all()
    )
    first_sale_date: dict[tuple[int, int], int] = {
        (r.cliente_id, r.sku_id): r.first_date for r in first_sale_rows
    }

    # ── Pre-cargar first_sale_date por sku_id desde SI historico (para DOS interno) ──
    first_sale_si_rows = (
        session.query(
            FactSalesIn.sku_id,
            func.min(FactSalesIn.date_id).label("first_date"),
        )
        .group_by(FactSalesIn.sku_id)
        .all()
    )
    first_sale_si: dict[int, int] = {
        r.sku_id: r.first_date for r in first_sale_si_rows
    }

    # ── Pre-cargar SO historico: {(cliente_id, sku_id, date_id): ventas} ──
    so_hist_rows = (
        session.query(
            FactSalesOut.cliente_id,
            FactSalesOut.sku_id,
            FactSalesOut.date_id,
            func.sum(FactSalesOut.unidades_sales_out).label("total"),
        )
        .filter(FactSalesOut.date_id.in_(hist_ids))
        .group_by(FactSalesOut.cliente_id, FactSalesOut.sku_id, FactSalesOut.date_id)
        .all()
    )
    so_hist: dict[tuple[int, int, int], float] = {
        (r.cliente_id, r.sku_id, r.date_id): float(r.total or 0)
        for r in so_hist_rows
    }

    # ── Pre-cargar SO constrained (proyectado) ──
    so_constr_rows = (
        session.query(
            FactSalesOutConstrained.cliente_id,
            FactSalesOutConstrained.sku_id,
            FactSalesOutConstrained.date_id,
            FactSalesOutConstrained.unidades_sales_out_constr,
        )
        .filter(FactSalesOutConstrained.date_id.in_(projection_ids))
        .all()
    )
    so_constr: dict[tuple[int, int, int], float] = {
        (r.cliente_id, r.sku_id, r.date_id): float(r.unidades_sales_out_constr or 0)
        for r in so_constr_rows
    }

    # ── Pre-cargar inv cliente constrained ──
    inv_cli_rows = (
        session.query(
            FactInventoryClientConstrained.cliente_id,
            FactInventoryClientConstrained.sku_id,
            FactInventoryClientConstrained.date_id,
            FactInventoryClientConstrained.inventario_final_constr,
        )
        .filter(FactInventoryClientConstrained.date_id.in_(projection_ids))
        .all()
    )
    inv_cli: dict[tuple[int, int, int], float] = {
        (r.cliente_id, r.sku_id, r.date_id): float(r.inventario_final_constr or 0)
        for r in inv_cli_rows
    }

    # ── Pre-cargar SI historico AGREGADO por (sku_id, date_id) ──
    si_hist_rows = (
        session.query(
            FactSalesIn.sku_id,
            FactSalesIn.date_id,
            func.sum(FactSalesIn.unidades_sales_in).label("total"),
        )
        .filter(FactSalesIn.date_id.in_(hist_ids))
        .group_by(FactSalesIn.sku_id, FactSalesIn.date_id)
        .all()
    )
    si_hist_agg: dict[tuple[int, int], float] = {
        (r.sku_id, r.date_id): float(r.total or 0) for r in si_hist_rows
    }

    # ── Pre-cargar SI constrained AGREGADO por (sku_id, date_id) ──
    si_constr_agg_rows = (
        session.query(
            FactSalesInConstrained.sku_id,
            FactSalesInConstrained.date_id,
            func.sum(FactSalesInConstrained.unidades_sales_in_constr).label("total"),
        )
        .filter(FactSalesInConstrained.date_id.in_(projection_ids))
        .group_by(FactSalesInConstrained.sku_id, FactSalesInConstrained.date_id)
        .all()
    )
    si_constr_agg: dict[tuple[int, int], float] = {
        (r.sku_id, r.date_id): float(r.total or 0) for r in si_constr_agg_rows
    }

    # ── Pre-cargar inv interno constrained ──
    inv_int_rows = (
        session.query(
            FactInventoryInternalConstrained.sku_id,
            FactInventoryInternalConstrained.date_id,
            FactInventoryInternalConstrained.inventario_final_int,
        )
        .filter(FactInventoryInternalConstrained.date_id.in_(projection_ids))
        .all()
    )
    inv_int: dict[tuple[int, int], float] = {
        (r.sku_id, r.date_id): float(r.inventario_final_int or 0)
        for r in inv_int_rows
    }

    # ── Limpiar datos previos ──
    session.query(FactDosConstraintCliente).filter(
        FactDosConstraintCliente.date_id.in_(projection_ids),
    ).delete(synchronize_session="fetch")
    session.query(FactDosConstraintInterno).filter(
        FactDosConstraintInterno.date_id.in_(projection_ids),
    ).delete(synchronize_session="fetch")

    # ── DOS CLIENTE ──
    dos_cli_count = 0

    # Obtener pares unicos (cliente, sku) con inv_cli data
    cli_pairs = set((k[0], k[1]) for k in inv_cli.keys())

    for cliente_id, sku_id in cli_pairs:
        fd = first_sale_date.get((cliente_id, sku_id))

        for t in range(len(projection_ids)):
            date_id_t = projection_ids[t]
            inv_val = inv_cli.get((cliente_id, sku_id, date_id_t), 0.0)

            # Trailing 12 meses ANTES del periodo t
            prev_12_start = _compute_month_minus_n(date_id_t, 12)
            prev_12_end = _compute_month_minus_n(date_id_t, 1)
            prev_12_ids = _generate_date_id_range(prev_12_start, prev_12_end)

            # Ajustar por first_sale_date
            if fd is not None and fd > prev_12_start:
                prev_12_ids = [d for d in prev_12_ids if d >= fd]

            if not prev_12_ids:
                session.add(
                    FactDosConstraintCliente(
                        date_id=date_id_t,
                        cliente_id=cliente_id,
                        sku_id=sku_id,
                        inventario_final_constr=float(round(inv_val, 4)),
                        promedio_ventas_12m=None,
                        months_of_sale_constr=None,
                        days_of_sale_constr=None,
                    )
                )
                dos_cli_count += 1
                continue

            # Sumar ventas de la ventana (hist + proyectado)
            total_ventas = 0.0
            for did in prev_12_ids:
                if did <= mes_cierre_date_id:
                    # Mes historico → SO historico
                    total_ventas += so_hist.get((cliente_id, sku_id, did), 0.0)
                else:
                    # Mes proyectado → SO constrained
                    total_ventas += so_constr.get((cliente_id, sku_id, did), 0.0)

            n_meses = len(prev_12_ids)
            promedio = total_ventas / n_meses if n_meses > 0 else 0.0

            if promedio > 0:
                mos = inv_val / promedio
                dos = mos * 30
            else:
                mos = None
                dos = None

            session.add(
                FactDosConstraintCliente(
                    date_id=date_id_t,
                    cliente_id=cliente_id,
                    sku_id=sku_id,
                    inventario_final_constr=float(round(inv_val, 4)),
                    promedio_ventas_12m=(
                        float(round(promedio, 4)) if promedio > 0 else None
                    ),
                    months_of_sale_constr=(
                        float(round(mos, 4)) if mos is not None else None
                    ),
                    days_of_sale_constr=(
                        float(round(dos, 4)) if dos is not None else None
                    ),
                )
            )
            dos_cli_count += 1

    # ── DOS INTERNO ──
    dos_int_count = 0

    # SKUs con inventario interno constrained
    int_skus = set(k[0] for k in inv_int.keys())

    for sku_id in int_skus:
        fd_si = first_sale_si.get(sku_id)

        for t in range(len(projection_ids)):
            date_id_t = projection_ids[t]
            inv_val = inv_int.get((sku_id, date_id_t), 0.0)

            # Trailing 12 meses ANTES del periodo t
            prev_12_start = _compute_month_minus_n(date_id_t, 12)
            prev_12_end = _compute_month_minus_n(date_id_t, 1)
            prev_12_ids = _generate_date_id_range(prev_12_start, prev_12_end)

            # Ajustar por first_sale_date (SI historico)
            if fd_si is not None and fd_si > prev_12_start:
                prev_12_ids = [d for d in prev_12_ids if d >= fd_si]

            if not prev_12_ids:
                session.add(
                    FactDosConstraintInterno(
                        date_id=date_id_t,
                        sku_id=sku_id,
                        inventario_final_int_constr=float(round(inv_val, 4)),
                        promedio_ventas_12m=None,
                        months_of_sale_constr=None,
                        days_of_sale_constr=None,
                    )
                )
                dos_int_count += 1
                continue

            # Sumar SI de la ventana (hist agregado + proj agregado)
            total_ventas = 0.0
            for did in prev_12_ids:
                if did <= mes_cierre_date_id:
                    total_ventas += si_hist_agg.get((sku_id, did), 0.0)
                else:
                    total_ventas += si_constr_agg.get((sku_id, did), 0.0)

            n_meses = len(prev_12_ids)
            promedio = total_ventas / n_meses if n_meses > 0 else 0.0

            if promedio > 0:
                mos = inv_val / promedio
                dos = mos * 30
            else:
                mos = None
                dos = None

            session.add(
                FactDosConstraintInterno(
                    date_id=date_id_t,
                    sku_id=sku_id,
                    inventario_final_int_constr=float(round(inv_val, 4)),
                    promedio_ventas_12m=(
                        float(round(promedio, 4)) if promedio > 0 else None
                    ),
                    months_of_sale_constr=(
                        float(round(mos, 4)) if mos is not None else None
                    ),
                    days_of_sale_constr=(
                        float(round(dos, 4)) if dos is not None else None
                    ),
                )
            )
            dos_int_count += 1

    session.flush()
    logger.info(
        "compute_dos_constraint: %d registros DOS cliente, %d registros DOS interno",
        dos_cli_count,
        dos_int_count,
    )
    return {
        "dos_cliente_count": dos_cli_count,
        "dos_interno_count": dos_int_count,
    }


# ─────────────────────────────────────────────
# ORQUESTADOR FASE B + C
# ─────────────────────────────────────────────


def run_phase_b_and_c(
    session: Session,
    mes_cierre_date_id: int,
) -> dict:
    """
    Ejecuta Fases B y C del Constraint Demand de forma encadenada.

    Secuencia:
    1. Sales Out Constrained + Inventario cliente Constrained (Fase B).
    2. Reporte ventas perdidas Sales Out (Fase C.1).
    3. Reporte ventas perdidas Sales In (Fase C.2).
    4. DOS Constraint cliente + interno (Fase C.3).

    Pre-condicion: fact_sales_in_constrained ya actualizado con valores aprobados.

    Args:
        session: Sesion de SQLAlchemy activa.
        mes_cierre_date_id: date_id del ultimo mes historico (YYYYMM).

    Returns:
        dict con resumen completo de Fases B y C.
    """
    start_time = datetime.now()
    summary = {
        "mes_cierre_date_id": mes_cierre_date_id,
        "fase": "CONSTRAINT_B_C",
        "estado": "EN_PROCESO",
        "pasos": {},
    }

    try:
        # Paso B.1: Sales Out Constrained + Inventario cliente
        logger.info("Fase B: Calculando Sales Out Constrained...")
        so_result = compute_sales_out_constrained(session, mes_cierre_date_id)
        summary["pasos"]["B1_sales_out_constrained"] = so_result

        # Paso C.1: Reporte ventas perdidas SO
        logger.info("Fase C.1: Generando reporte ventas perdidas Sales Out...")
        lost_so = generate_lost_sales_report_so(session, mes_cierre_date_id)
        summary["pasos"]["C1_lost_sales_so"] = {"registros": lost_so}

        # Paso C.2: Reporte ventas perdidas SI
        logger.info("Fase C.2: Generando reporte ventas perdidas Sales In...")
        lost_si = generate_lost_sales_report_si(session, mes_cierre_date_id)
        summary["pasos"]["C2_lost_sales_si"] = {"registros": lost_si}

        # Paso C.3: DOS Constraint
        logger.info("Fase C.3: Calculando DOS Constraint...")
        dos_result = compute_dos_constraint(session, mes_cierre_date_id)
        summary["pasos"]["C3_dos_constraint"] = dos_result

        summary["estado"] = "COMPLETADO"
        summary["mensaje"] = (
            f"Fases B y C completadas. "
            f"SO Constrained: {so_result['so_constrained_count']} registros "
            f"({so_result['adjustments']} ajustes). "
            f"Ventas perdidas: {lost_so} SO, {lost_si} SI. "
            f"DOS: {dos_result['dos_cliente_count']} cliente, "
            f"{dos_result['dos_interno_count']} interno."
        )
        summary["duracion_segundos"] = (datetime.now() - start_time).total_seconds()

        session.commit()
        logger.info(
            "run_phase_b_and_c completado para mes %d", mes_cierre_date_id
        )
        return summary

    except Exception as e:
        session.rollback()
        summary["estado"] = "ERROR"
        summary["mensaje"] = f"Error en Fases B/C: {str(e)}"
        summary["duracion_segundos"] = (datetime.now() - start_time).total_seconds()
        logger.exception(
            "Error en run_phase_b_and_c para mes %d", mes_cierre_date_id
        )
        raise
