"""
constraint_phase_b.py — Phase B: Sales Out Constraint + Client Inventory Constraint.
                        Phase C: Lost Sales report + DOS Constraint.

Phase B (§5):
    SO_constr = SO_unc when inv_tentative >= 0.
    SO_constr = SO_unc - (SI_unc - SI_constr) when inv_tentative < 0.
    Fallback: SO_constr = inv_prev + SI_constr (sell only available).
    Client inventory NEVER negative after adjustment.

Phase C:
    Lost sales report: SI and SO differences where lost > 0.
    DOS Constraint: client-level and internal-level, using trailing 12m window.

Conventions:
    - Round all numeric values to 4 decimals before DB storage.
    - Never call session.commit() inside business logic. Only API endpoints commit.
    - Use session.flush() after batch inserts.
    - Add brief comment citing rule on every formula line.
"""

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from .constraint_demand import (
    _get_lead_time,
    _get_projection_ids,
)
from .eligibility import _generate_date_id_range
from .models import (
    DimCliente,
    DimSku,
    FactInventoryClienteConstrained,
    FactInventoryInternalConstrained,
    FactSalesIn,
    FactSalesInConstrained,
    FactSalesInUnconstrained,
    FactSalesOut,
    FactSalesOutConstrained,
    FactSalesOutUnconstrained,
    FactStockCliente,
)
from .monthly_close import _compute_month_minus_n

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# STEP 11: PHASE B — SALES OUT CONSTRAINT (§5)
# ─────────────────────────────────────────────


def run_sales_out_constraint(
    session: Session,
    mes_cierre_date_id: int,
) -> dict:
    """
    Phase B: Compute Sales Out Constrained + Client Inventory Constrained.

    Performance: Pre-loads ALL data into lookup dicts before the main loop.

    Math (§5) per client-SKU pair, iterating t=0..11:
        For t < L:
            inv_tentative = inv_prev + SI_constr - SO_unc  # §5: tentative inv
            if inv_tentative >= 0:
                SO_constr = SO_unc  # §5: no adjustment needed
                inv_client = inv_tentative
            else:
                SO_constr = SO_unc - (SI_unc - SI_constr)  # §5: adjust by SI gap
                inv_client = inv_prev + SI_constr - SO_constr
                if inv_client < -0.01:
                    raise ValueError(...)  # §5: client inv never negative

        For t >= L:
            SO_constr = SO_unc  # §5: no constraint after lead time
            inv_client = inv_prev + SI_constr - SO_constr

        inv_prev = inv_client

    Returns:
        {"so_constrained_count": int, "inv_client_count": int}
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)

    # ── Pre-load ALL data into lookup dicts ──

    # SI Constrained: {(cid, sid, did): val}
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
    si_constr_lk: dict[tuple[int, int, int], float] = {
        (r.cliente_id, r.sku_id, r.date_id): float(r.unidades_sales_in_constr or 0)
        for r in si_constr_rows
    }

    # SI Unconstrained: {(cid, sid, did): val}
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
    si_unc_lk: dict[tuple[int, int, int], float] = {
        (r.cliente_id, r.sku_id, r.date_id): float(r.unidades_sales_in_unc or 0)
        for r in si_unc_rows
    }

    # SO Unconstrained: {(cid, sid, did): val}
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
    so_unc_lk: dict[tuple[int, int, int], float] = {
        (r.cliente_id, r.sku_id, r.date_id): float(r.unidades_sales_out_unc or 0)
        for r in so_unc_rows
    }

    # Client inventory initial: {(cid, sid): val}
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
    inv_client_initial: dict[tuple[int, int], float] = {
        (r.cliente_id, r.sku_id): float(r.total or 0) for r in inv_hist_rows
    }

    # ── Determine unique (cliente_id, sku_id) pairs ──
    pairs = set()
    for key in si_constr_lk:
        pairs.add((key[0], key[1]))
    for key in so_unc_lk:
        pairs.add((key[0], key[1]))

    # ── Clean previous records ──
    session.query(FactSalesOutConstrained).filter(
        FactSalesOutConstrained.date_id.in_(projection_ids),
    ).delete(synchronize_session="fetch")
    session.query(FactInventoryClienteConstrained).filter(
        FactInventoryClienteConstrained.date_id.in_(projection_ids),
    ).delete(synchronize_session="fetch")

    so_count = 0
    inv_count = 0

    for cliente_id, sku_id in sorted(pairs):
        L = _get_lead_time(session, sku_id)
        inv_prev = inv_client_initial.get((cliente_id, sku_id), 0.0)

        for t in range(len(projection_ids)):
            date_id_t = projection_ids[t]
            key = (cliente_id, sku_id, date_id_t)

            si_constr_t = si_constr_lk.get(key, 0.0)
            si_unc_t = si_unc_lk.get(key, 0.0)
            so_unc_t = so_unc_lk.get(key, 0.0)

            if t < L:
                inv_tentative = inv_prev + si_constr_t - so_unc_t  # §5: tentative inv

                if inv_tentative >= 0:
                    so_constr_t = so_unc_t  # §5: no adjustment needed
                    inv_client = inv_tentative  # §5: tentative becomes final
                else:
                    so_constr_t = so_unc_t - (si_unc_t - si_constr_t)  # §5: adjust by SI gap
                    inv_client = inv_prev + si_constr_t - so_constr_t  # §5: recalc inv

                    if inv_client < -0.01:
                        # §5: client inv never negative — fallback
                        so_constr_t = max(inv_prev + si_constr_t, 0.0)  # §5: sell only available
                        inv_client = 0.0  # §5: floor at zero

                # §5: SO_constr cannot be negative
                if so_constr_t < 0:
                    so_constr_t = 0.0
            else:
                so_constr_t = so_unc_t  # §5: no constraint after lead time
                inv_client = inv_prev + si_constr_t - so_constr_t  # §5: normal flow

            session.add(FactSalesOutConstrained(
                date_id=date_id_t,
                cliente_id=cliente_id,
                sku_id=sku_id,
                unidades_sales_out_constr=float(round(so_constr_t, 4)),
            ))
            so_count += 1

            session.add(FactInventoryClienteConstrained(
                date_id=date_id_t,
                cliente_id=cliente_id,
                sku_id=sku_id,
                inventario_final_cliente_constr=float(round(inv_client, 4)),
            ))
            inv_count += 1

            inv_prev = inv_client

    session.flush()
    logger.info(
        "run_sales_out_constraint: %d SO_constr, %d inv_client records for %d pairs",
        so_count, inv_count, len(pairs),
    )
    return {"so_constrained_count": so_count, "inv_client_count": inv_count}


# ─────────────────────────────────────────────
# STEP 13: PHASE C — LOST SALES REPORT
# ─────────────────────────────────────────────


def generate_lost_sales_report(
    session: Session,
    mes_cierre_date_id: int,
) -> list[dict]:
    """
    Query SI_unc, SI_constr, SO_unc, SO_constr for all 12 projection months.
    Compute lost_si = si_unc - si_constr, lost_so = so_unc - so_constr.
    Return only rows where lost_si > 0 or lost_so > 0.
    Sorted by sku_id, cliente_id, date_id.
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)

    # Pre-load all lookup dicts
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
    si_unc_lk: dict[tuple[int, int, int], float] = {
        (r.cliente_id, r.sku_id, r.date_id): float(r.unidades_sales_in_unc or 0)
        for r in si_unc_rows
    }

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
    si_constr_lk: dict[tuple[int, int, int], float] = {
        (r.cliente_id, r.sku_id, r.date_id): float(r.unidades_sales_in_constr or 0)
        for r in si_constr_rows
    }

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
    so_unc_lk: dict[tuple[int, int, int], float] = {
        (r.cliente_id, r.sku_id, r.date_id): float(r.unidades_sales_out_unc or 0)
        for r in so_unc_rows
    }

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
    so_constr_lk: dict[tuple[int, int, int], float] = {
        (r.cliente_id, r.sku_id, r.date_id): float(r.unidades_sales_out_constr or 0)
        for r in so_constr_rows
    }

    # Pre-load SKU info and client names
    sku_info: dict[int, dict] = {}
    for s in session.query(DimSku).all():
        sku_info[s.sku_id] = {
            "upc": s.upc,
            "familia": s.familia,
        }

    client_names: dict[int, str] = {}
    for c in session.query(DimCliente).all():
        client_names[c.cliente_id] = c.cliente_nombre

    # Combine all keys
    all_keys = set(si_unc_lk.keys()) | set(so_unc_lk.keys())

    result: list[dict] = []
    for cliente_id, sku_id, date_id in sorted(all_keys, key=lambda k: (k[1], k[0], k[2])):
        si_unc_v = si_unc_lk.get((cliente_id, sku_id, date_id), 0.0)
        si_constr_v = si_constr_lk.get((cliente_id, sku_id, date_id), 0.0)
        so_unc_v = so_unc_lk.get((cliente_id, sku_id, date_id), 0.0)
        so_constr_v = so_constr_lk.get((cliente_id, sku_id, date_id), 0.0)

        lost_si = round(si_unc_v - si_constr_v, 4)
        lost_so = round(so_unc_v - so_constr_v, 4)

        if lost_si > 0 or lost_so > 0:
            info = sku_info.get(sku_id, {"upc": None, "familia": None})
            result.append({
                "sku_id": sku_id,
                "upc": int(info["upc"]) if info["upc"] else None,
                "familia": info["familia"],
                "cliente_id": cliente_id,
                "cliente_nombre": client_names.get(cliente_id, f"CLIENTE_{cliente_id}"),
                "date_id": date_id,
                "lost_si": max(lost_si, 0.0),
                "lost_so": max(lost_so, 0.0),
            })

    logger.info("generate_lost_sales_report: %d rows with losses", len(result))
    return result


# ─────────────────────────────────────────────
# STEP 14: PHASE C — DOS CONSTRAINT
# ─────────────────────────────────────────────


def compute_dos_constraint(
    session: Session,
    mes_cierre_date_id: int,
) -> dict:
    """
    For internal inventory: query FactInventoryInternalConstrained, compute DOS
    using SO constrained data (aggregated by SKU).
    Formula: DOS = (inv / avg_monthly_SO_12m) * 30

    For client inventory: query FactInventoryClienteConstrained, compute DOS
    per client-SKU. Formula: same, using per-client SO constrained.

    Update days_of_sale_int_constr and days_of_sale_cli_constr columns.

    Trailing 12m uses available historical SO + projected SO constrained.

    Returns:
        {"internal_dos_updated": int, "client_dos_updated": int}
    """
    projection_ids = _get_projection_ids(mes_cierre_date_id)

    # Historical window: 12 months up to close
    hist_start = _compute_month_minus_n(mes_cierre_date_id, 11)
    hist_ids = _generate_date_id_range(hist_start, mes_cierre_date_id)

    # ── Pre-load historical SO by (cliente, sku, date) ──
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
    so_hist_cli: dict[tuple[int, int, int], float] = {
        (r.cliente_id, r.sku_id, r.date_id): float(r.total or 0)
        for r in so_hist_rows
    }

    # Aggregated historical SO by (sku, date) for internal DOS
    so_hist_agg: dict[tuple[int, int], float] = {}
    for (cid, sid, did), val in so_hist_cli.items():
        key = (sid, did)
        so_hist_agg[key] = so_hist_agg.get(key, 0.0) + val

    # ── Pre-load projected SO constrained by (cliente, sku, date) ──
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
    so_constr_cli: dict[tuple[int, int, int], float] = {
        (r.cliente_id, r.sku_id, r.date_id): float(r.unidades_sales_out_constr or 0)
        for r in so_constr_rows
    }

    # Aggregated projected SO constrained by (sku, date)
    so_constr_agg: dict[tuple[int, int], float] = {}
    for (cid, sid, did), val in so_constr_cli.items():
        key = (sid, did)
        so_constr_agg[key] = so_constr_agg.get(key, 0.0) + val

    # ── Pre-load historical SI aggregated by (sku, date) for internal DOS ──
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

    # SI constrained aggregated by (sku, date)
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

    # ── Pre-load first_sale_date ──
    first_sale_cli_rows = (
        session.query(
            FactSalesOut.cliente_id,
            FactSalesOut.sku_id,
            func.min(FactSalesOut.date_id).label("first_date"),
        )
        .group_by(FactSalesOut.cliente_id, FactSalesOut.sku_id)
        .all()
    )
    first_sale_cli: dict[tuple[int, int], int] = {
        (r.cliente_id, r.sku_id): r.first_date for r in first_sale_cli_rows
    }

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

    # Helper: compute trailing 12m DOS
    def _compute_dos(
        inv_val: float,
        date_id_t: int,
        first_date: int | None,
        get_sales_fn,
    ) -> float | None:
        """
        DOS = (inv / avg_monthly_sales) * 30
        avg_monthly_sales = total_sales_trailing_12m / N
        N = min(12, months since first_sale_date)
        """
        # Trailing 12 months BEFORE the current period
        prev_12_start = _compute_month_minus_n(date_id_t, 12)
        prev_12_end = _compute_month_minus_n(date_id_t, 1)
        prev_12_ids = _generate_date_id_range(prev_12_start, prev_12_end)

        # Adjust by first_sale_date
        if first_date is not None and first_date > prev_12_start:
            prev_12_ids = [d for d in prev_12_ids if d >= first_date]

        if not prev_12_ids:
            return None

        total_sales = sum(get_sales_fn(d) for d in prev_12_ids)
        n_months = len(prev_12_ids)

        if n_months > 0 and total_sales > 0:
            avg_monthly = total_sales / n_months  # §: DOS formula
            mos = inv_val / avg_monthly  # §: months of sale
            return round(mos * 30, 4)  # §: days of sale
        return None

    # ── CLIENT DOS ──
    client_dos_count = 0

    # Get all client inventory constrained records
    inv_cli_rows = (
        session.query(FactInventoryClienteConstrained)
        .filter(FactInventoryClienteConstrained.date_id.in_(projection_ids))
        .all()
    )

    for rec in inv_cli_rows:
        inv_val = float(rec.inventario_final_cliente_constr or 0)
        fd = first_sale_cli.get((rec.cliente_id, rec.sku_id))

        def get_so_client(did, _cid=rec.cliente_id, _sid=rec.sku_id):
            if did <= mes_cierre_date_id:
                return so_hist_cli.get((_cid, _sid, did), 0.0)
            else:
                return so_constr_cli.get((_cid, _sid, did), 0.0)

        dos = _compute_dos(inv_val, rec.date_id, fd, get_so_client)
        rec.days_of_sale_cli_constr = dos if dos is not None else None
        client_dos_count += 1

    # ── INTERNAL DOS ──
    internal_dos_count = 0

    inv_int_rows = (
        session.query(FactInventoryInternalConstrained)
        .filter(FactInventoryInternalConstrained.date_id.in_(projection_ids))
        .all()
    )

    for rec in inv_int_rows:
        inv_val = float(rec.inventario_final_int or 0)
        fd = first_sale_si.get(rec.sku_id)

        def get_si_internal(did, _sid=rec.sku_id):
            if did <= mes_cierre_date_id:
                return si_hist_agg.get((_sid, did), 0.0)
            else:
                return si_constr_agg.get((_sid, did), 0.0)

        dos = _compute_dos(inv_val, rec.date_id, fd, get_si_internal)
        rec.days_of_sale_int_constr = dos if dos is not None else None
        internal_dos_count += 1

    session.flush()
    logger.info(
        "compute_dos_constraint: %d client DOS, %d internal DOS updated",
        client_dos_count, internal_dos_count,
    )
    return {
        "internal_dos_updated": internal_dos_count,
        "client_dos_updated": client_dos_count,
    }
