"""
api_frontend.py — Endpoints adicionales para la interfaz React.

Todos bajo el prefijo /api para separar de los endpoints originales.
Proveen datos pre-agregados para las vistas de reportes, catalogo
y configuracion.
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, distinct, case, and_
from sqlalchemy.orm import Session

from .database import get_db
from .models import (
    DimCliente,
    DimSku,
    DimTiempo,
    FactInventoryUnconstrained,
    FactSalesInConstrained,
    FactSalesInUnconstrained,
    FactSalesOutConstrained,
    FactSalesOutUnconstrained,
    RptAsignacionInventario,
    RptLostSalesOos,
    RptLostSalesSalesIn,
    RptLostSalesSalesOut,
)
from .monthly_close import _compute_month_minus_n

router = APIRouter(prefix="/api", tags=["Frontend"])


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _projection_ids(mes_cierre: int) -> list[int]:
    """Genera los 12 date_ids de proyeccion posteriores al cierre."""
    from .eligibility import _generate_date_id_range
    from .monthly_close import _compute_month_plus_n
    start = _compute_month_plus_n(mes_cierre, 1)
    end = _compute_month_plus_n(mes_cierre, 12)
    return _generate_date_id_range(start, end)


# ─────────────────────────────────────────────
# REPORTES — Unconstrained Summary
# ─────────────────────────────────────────────


@router.get("/reports/available-months")
def available_months(db: Session = Depends(get_db)):
    """
    FIX BUG-02: Retorna los meses de cierre disponibles (date_ids en dim_tiempo)
    para poblar dinamicamente el selector de mes en el frontend.
    """
    rows = (
        db.query(DimTiempo.date_id, DimTiempo.anio, DimTiempo.mes_numero)
        .order_by(DimTiempo.date_id.desc())
        .all()
    )
    meses_nombre = {
        1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
        5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
        9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
    }
    return [
        {
            "date_id": r.date_id,
            "label": f"{meses_nombre.get(r.mes_numero, r.mes_numero)} {r.anio} ({r.date_id})",
        }
        for r in rows
    ]


@router.get("/reports/unconstrained-summary/{date_id}")
def unconstrained_summary(
    date_id: int,
    cliente: str | None = None,
    familia: str | None = None,
    db: Session = Depends(get_db),
):
    """
    KPIs + tablas por familia y por cliente para Unconstrained Demand.
    FIX BUG-01: Acepta filtros opcionales de cliente y familia.
    """
    proj_ids = _projection_ids(date_id)

    # FIX BUG-01: Construir set de sku_ids filtrados por familia
    sku_filter_ids = None
    if familia:
        sku_rows = db.query(DimSku.sku_id).filter(DimSku.familia == familia).all()
        sku_filter_ids = [r.sku_id for r in sku_rows]
        if not sku_filter_ids:
            return {"kpis": {"ventas_unc_12m": 0, "inventario_objetivo_dias_promedio": None, "clientes_riesgo_sobreinventario": 0}, "by_familia": [], "by_cliente": []}

    # FIX BUG-01: Construir cliente_id filtrado
    cliente_filter_id = None
    if cliente:
        cli_row = db.query(DimCliente.cliente_id).filter(DimCliente.cliente_nombre == cliente).first()
        if not cli_row:
            return {"kpis": {"ventas_unc_12m": 0, "inventario_objetivo_dias_promedio": None, "clientes_riesgo_sobreinventario": 0}, "by_familia": [], "by_cliente": []}
        cliente_filter_id = cli_row.cliente_id

    # ── Helper: condiciones de filtro opcionales ──
    def _apply_si_unc_filters(q):
        """Aplica filtros de cliente y familia a query sobre FactSalesInUnconstrained."""
        q = q.filter(FactSalesInUnconstrained.date_id.in_(proj_ids))
        if sku_filter_ids is not None:
            q = q.filter(FactSalesInUnconstrained.sku_id.in_(sku_filter_ids))
        if cliente_filter_id is not None:
            q = q.filter(FactSalesInUnconstrained.cliente_id == cliente_filter_id)
        return q

    def _apply_inv_unc_filters(q):
        """Aplica filtros a query sobre FactInventoryUnconstrained."""
        if sku_filter_ids is not None:
            q = q.filter(FactInventoryUnconstrained.sku_id.in_(sku_filter_ids))
        if cliente_filter_id is not None:
            q = q.filter(FactInventoryUnconstrained.cliente_id == cliente_filter_id)
        return q

    # ── KPIs ──

    # Total ventas SI unconstrained 12M
    kpi_q = db.query(func.sum(FactSalesInUnconstrained.unidades_sales_in_unc))
    kpi_q = _apply_si_unc_filters(kpi_q)
    total_si_unc = kpi_q.scalar() or 0

    # DOS ponderado = (SUM inventario_final / (SUM sales_out_12m / 12)) * 30
    # NO usar AVG simple de DOS individuales — los outliers (SKUs con poco
    # movimiento y mucho inventario) inflan el promedio artificialmente.
    last_proj = proj_ids[-1] if proj_ids else None
    avg_dos = None
    if last_proj:
        inv_q = (
            db.query(func.sum(FactInventoryUnconstrained.inventario_final_unc))
            .filter(FactInventoryUnconstrained.date_id == last_proj)
        )
        inv_q = _apply_inv_unc_filters(inv_q)
        total_inv = float(inv_q.scalar() or 0)

        so_q = (
            db.query(func.sum(FactSalesOutUnconstrained.unidades_sales_out_unc))
            .filter(FactSalesOutUnconstrained.date_id.in_(proj_ids))
        )
        if sku_filter_ids is not None:
            so_q = so_q.filter(FactSalesOutUnconstrained.sku_id.in_(sku_filter_ids))
        if cliente_filter_id is not None:
            so_q = so_q.filter(FactSalesOutUnconstrained.cliente_id == cliente_filter_id)
        total_so_12m = float(so_q.scalar() or 0)

        if total_so_12m > 0:
            # Para datos proyectados, los 12 meses siempre estan poblados
            # para todos los pares elegibles, por lo que /12 es correcto.
            # (La logica de N ajustado por first_sale_date ya fue aplicada
            # a nivel SKU-cliente en unconstrained_demand._iterative_projection)
            avg_monthly_so = total_so_12m / 12
            avg_dos = round((total_inv / avg_monthly_so) * 30, 1)
        else:
            avg_dos = None

    # Clientes en riesgo de sobreinventario (MOS > 3 en ultimo mes)
    riesgo_count = 0
    if last_proj:
        riesgo_q = (
            db.query(FactInventoryUnconstrained.cliente_id)
            .filter(
                FactInventoryUnconstrained.date_id == last_proj,
                FactInventoryUnconstrained.months_of_sale_unc > 3,
            )
        )
        riesgo_q = _apply_inv_unc_filters(riesgo_q)
        riesgo_rows = riesgo_q.distinct().all()
        riesgo_count = len(riesgo_rows)

    # ── By Familia ──
    fam_q = (
        db.query(
            DimSku.familia.label("familia"),
            func.sum(FactSalesInUnconstrained.unidades_sales_in_unc).label("ventas_unc_12m"),
        )
        .join(DimSku, FactSalesInUnconstrained.sku_id == DimSku.sku_id)
    )
    fam_q = _apply_si_unc_filters(fam_q)
    by_familia = (
        fam_q
        .group_by(DimSku.familia)
        .order_by(func.sum(FactSalesInUnconstrained.unidades_sales_in_unc).desc())
        .all()
    )

    # Calcular inv objetivo por familia (ultimo mes)
    familia_inv = {}
    familia_so = {}
    if last_proj:
        inv_fam_q = (
            db.query(
                DimSku.familia.label("familia"),
                func.sum(FactInventoryUnconstrained.inventario_final_unc).label("inv"),
            )
            .join(DimSku, FactInventoryUnconstrained.sku_id == DimSku.sku_id)
            .filter(FactInventoryUnconstrained.date_id == last_proj)
        )
        inv_fam_q = _apply_inv_unc_filters(inv_fam_q)
        inv_by_fam = inv_fam_q.group_by(DimSku.familia).all()
        familia_inv = {r.familia: float(r.inv or 0) for r in inv_by_fam}

        so_fam_q = (
            db.query(
                DimSku.familia.label("familia"),
                func.sum(FactSalesOutUnconstrained.unidades_sales_out_unc).label("so"),
            )
            .join(DimSku, FactSalesOutUnconstrained.sku_id == DimSku.sku_id)
            .filter(FactSalesOutUnconstrained.date_id.in_(proj_ids))
        )
        if sku_filter_ids is not None:
            so_fam_q = so_fam_q.filter(FactSalesOutUnconstrained.sku_id.in_(sku_filter_ids))
        if cliente_filter_id is not None:
            so_fam_q = so_fam_q.filter(FactSalesOutUnconstrained.cliente_id == cliente_filter_id)
        so_by_fam = so_fam_q.group_by(DimSku.familia).all()
        familia_so = {r.familia: float(r.so or 0) for r in so_by_fam}

    familia_data = []
    for r in by_familia:
        inv = familia_inv.get(r.familia, 0)
        so_total = familia_so.get(r.familia, 0)
        # Datos proyectados: 12 meses siempre poblados para elegibles → /12 correcto
        avg_monthly = so_total / 12 if so_total > 0 else 0
        mos = inv / avg_monthly if avg_monthly > 0 else 0
        dos = mos * 30
        familia_data.append({
            "familia": r.familia or "(Sin familia)",
            "ventas_unc_12m": round(float(r.ventas_unc_12m or 0)),
            "inv_objetivo_meses": round(mos, 1),
            "inv_objetivo_dias": round(dos, 0),
        })

    # ── By Cliente ──
    cli_q = (
        db.query(
            DimCliente.cliente_nombre.label("cliente"),
            func.sum(FactSalesInUnconstrained.unidades_sales_in_unc).label("ventas_unc_12m"),
            func.count(distinct(FactSalesInUnconstrained.sku_id)).label("skus_activos"),
        )
        .join(DimCliente, FactSalesInUnconstrained.cliente_id == DimCliente.cliente_id)
    )
    cli_q = _apply_si_unc_filters(cli_q)
    by_cliente = (
        cli_q
        .group_by(DimCliente.cliente_nombre)
        .order_by(func.sum(FactSalesInUnconstrained.unidades_sales_in_unc).desc())
        .all()
    )

    # DOS ponderado por cliente = (SUM inv_cli / (SUM so_cli_12m / 12)) * 30
    # NO usar AVG simple de DOS individuales por SKU.
    cliente_dos = {}
    if last_proj:
        # Inventario por cliente en ultimo mes
        inv_cli_q = (
            db.query(
                DimCliente.cliente_nombre.label("cliente"),
                func.sum(FactInventoryUnconstrained.inventario_final_unc).label("inv"),
            )
            .join(DimCliente, FactInventoryUnconstrained.cliente_id == DimCliente.cliente_id)
            .filter(FactInventoryUnconstrained.date_id == last_proj)
        )
        inv_cli_q = _apply_inv_unc_filters(inv_cli_q)
        inv_by_cli = inv_cli_q.group_by(DimCliente.cliente_nombre).all()
        inv_cli_map = {r.cliente: float(r.inv or 0) for r in inv_by_cli}

        # Sales Out 12M por cliente
        so_cli_q = (
            db.query(
                DimCliente.cliente_nombre.label("cliente"),
                func.sum(FactSalesOutUnconstrained.unidades_sales_out_unc).label("so"),
            )
            .join(DimCliente, FactSalesOutUnconstrained.cliente_id == DimCliente.cliente_id)
            .filter(FactSalesOutUnconstrained.date_id.in_(proj_ids))
        )
        if sku_filter_ids is not None:
            so_cli_q = so_cli_q.filter(FactSalesOutUnconstrained.sku_id.in_(sku_filter_ids))
        if cliente_filter_id is not None:
            so_cli_q = so_cli_q.filter(FactSalesOutUnconstrained.cliente_id == cliente_filter_id)
        so_by_cli = so_cli_q.group_by(DimCliente.cliente_nombre).all()
        so_cli_map = {r.cliente: float(r.so or 0) for r in so_by_cli}

        for cli_name in inv_cli_map:
            inv_val = inv_cli_map.get(cli_name, 0)
            so_val = so_cli_map.get(cli_name, 0)
            if so_val > 0:
                # Datos proyectados: 12 meses siempre poblados → /12 correcto
                avg_monthly = so_val / 12
                cliente_dos[cli_name] = (inv_val / avg_monthly) * 30
            else:
                cliente_dos[cli_name] = 0

    cliente_data = [
        {
            "cliente": r.cliente,
            "ventas_unc_12m": round(float(r.ventas_unc_12m or 0)),
            "dos_promedio": round(cliente_dos.get(r.cliente, 0), 1),
            "skus_activos": r.skus_activos,
        }
        for r in by_cliente
    ]

    return {
        "kpis": {
            "ventas_unc_12m": round(float(total_si_unc)),
            "inventario_objetivo_dias_promedio": avg_dos,
            "clientes_riesgo_sobreinventario": riesgo_count,
        },
        "by_familia": familia_data,
        "by_cliente": cliente_data,
    }


# ─────────────────────────────────────────────
# REPORTES — Constrained Summary
# ─────────────────────────────────────────────


@router.get("/reports/constrained-summary/{date_id}")
def constrained_summary(
    date_id: int,
    cliente: str | None = None,
    familia: str | None = None,
    db: Session = Depends(get_db),
):
    """
    KPIs + tablas por familia y por cliente para Constrained Demand.
    FIX BUG-01: Acepta filtros opcionales de cliente y familia.
    """
    proj_ids = _projection_ids(date_id)

    # FIX BUG-01: Resolver filtros
    sku_filter_ids = None
    if familia:
        sku_rows = db.query(DimSku.sku_id).filter(DimSku.familia == familia).all()
        sku_filter_ids = [r.sku_id for r in sku_rows]
        if not sku_filter_ids:
            return {"kpis": {"ventas_constr_12m": 0, "ventas_perdidas_oos": 0, "skus_riesgo_oos": 0}, "by_familia": [], "by_cliente": []}

    cliente_filter_id = None
    if cliente:
        cli_row = db.query(DimCliente.cliente_id).filter(DimCliente.cliente_nombre == cliente).first()
        if not cli_row:
            return {"kpis": {"ventas_constr_12m": 0, "ventas_perdidas_oos": 0, "skus_riesgo_oos": 0}, "by_familia": [], "by_cliente": []}
        cliente_filter_id = cli_row.cliente_id

    # ── KPIs ──

    constr_kpi_q = (
        db.query(func.sum(FactSalesInConstrained.unidades_sales_in_constr))
        .filter(FactSalesInConstrained.date_id.in_(proj_ids))
    )
    if sku_filter_ids is not None:
        constr_kpi_q = constr_kpi_q.filter(FactSalesInConstrained.sku_id.in_(sku_filter_ids))
    if cliente_filter_id is not None:
        constr_kpi_q = constr_kpi_q.filter(FactSalesInConstrained.cliente_id == cliente_filter_id)
    total_si_constr = constr_kpi_q.scalar() or 0

    unc_kpi_q = (
        db.query(func.sum(FactSalesInUnconstrained.unidades_sales_in_unc))
        .filter(FactSalesInUnconstrained.date_id.in_(proj_ids))
    )
    if sku_filter_ids is not None:
        unc_kpi_q = unc_kpi_q.filter(FactSalesInUnconstrained.sku_id.in_(sku_filter_ids))
    if cliente_filter_id is not None:
        unc_kpi_q = unc_kpi_q.filter(FactSalesInUnconstrained.cliente_id == cliente_filter_id)
    total_si_unc = unc_kpi_q.scalar() or 0

    lost_q = db.query(RptLostSalesOos)
    if sku_filter_ids is not None:
        lost_q = lost_q.filter(RptLostSalesOos.sku_id.in_(sku_filter_ids))
    lost_rows = lost_q.all()
    total_lost = sum(float(r.lost_sales_total or 0) for r in lost_rows)
    skus_oos = len(lost_rows)

    # ── By Familia ──

    # Unconstrained por familia
    unc_fam_q = (
        db.query(
            DimSku.familia.label("familia"),
            func.sum(FactSalesInUnconstrained.unidades_sales_in_unc).label("unc"),
        )
        .join(DimSku, FactSalesInUnconstrained.sku_id == DimSku.sku_id)
        .filter(FactSalesInUnconstrained.date_id.in_(proj_ids))
    )
    if sku_filter_ids is not None:
        unc_fam_q = unc_fam_q.filter(FactSalesInUnconstrained.sku_id.in_(sku_filter_ids))
    if cliente_filter_id is not None:
        unc_fam_q = unc_fam_q.filter(FactSalesInUnconstrained.cliente_id == cliente_filter_id)
    unc_by_fam = unc_fam_q.group_by(DimSku.familia).all()
    unc_fam_map = {r.familia: float(r.unc or 0) for r in unc_by_fam}

    # Constrained por familia
    constr_fam_q = (
        db.query(
            DimSku.familia.label("familia"),
            func.sum(FactSalesInConstrained.unidades_sales_in_constr).label("constr"),
        )
        .join(DimSku, FactSalesInConstrained.sku_id == DimSku.sku_id)
        .filter(FactSalesInConstrained.date_id.in_(proj_ids))
    )
    if sku_filter_ids is not None:
        constr_fam_q = constr_fam_q.filter(FactSalesInConstrained.sku_id.in_(sku_filter_ids))
    if cliente_filter_id is not None:
        constr_fam_q = constr_fam_q.filter(FactSalesInConstrained.cliente_id == cliente_filter_id)
    constr_by_fam = constr_fam_q.group_by(DimSku.familia).all()
    constr_fam_map = {r.familia: float(r.constr or 0) for r in constr_by_fam}

    # Lost sales por familia (via join con dim_sku)
    lost_fam_q = (
        db.query(
            DimSku.familia.label("familia"),
            func.sum(RptLostSalesOos.lost_sales_total).label("lost"),
        )
        .join(DimSku, RptLostSalesOos.sku_id == DimSku.sku_id)
    )
    if sku_filter_ids is not None:
        lost_fam_q = lost_fam_q.filter(RptLostSalesOos.sku_id.in_(sku_filter_ids))
    lost_by_fam = lost_fam_q.group_by(DimSku.familia).all()
    lost_fam_map = {r.familia: float(r.lost or 0) for r in lost_by_fam}

    all_familias = sorted(set(list(unc_fam_map.keys()) + list(constr_fam_map.keys())))
    familia_data = []
    for fam in all_familias:
        unc = unc_fam_map.get(fam, 0)
        constr = constr_fam_map.get(fam, 0)
        lost = lost_fam_map.get(fam, 0)
        pct = (lost / unc * 100) if unc > 0 else 0
        familia_data.append({
            "familia": fam or "(Sin familia)",
            "ventas_unc_12m": round(unc),
            "ventas_constr_12m": round(constr),
            "ventas_perdidas": round(lost),
            "pct_perdida": round(pct, 1),
        })

    # Ordenar por ventas_unc desc
    familia_data.sort(key=lambda x: x["ventas_unc_12m"], reverse=True)

    # ── By Cliente ──

    constr_cli_q = (
        db.query(
            DimCliente.cliente_nombre.label("cliente"),
            func.sum(FactSalesInConstrained.unidades_sales_in_constr).label("constr"),
        )
        .join(DimCliente, FactSalesInConstrained.cliente_id == DimCliente.cliente_id)
        .filter(FactSalesInConstrained.date_id.in_(proj_ids))
    )
    if sku_filter_ids is not None:
        constr_cli_q = constr_cli_q.filter(FactSalesInConstrained.sku_id.in_(sku_filter_ids))
    if cliente_filter_id is not None:
        constr_cli_q = constr_cli_q.filter(FactSalesInConstrained.cliente_id == cliente_filter_id)
    constr_by_cli = (
        constr_cli_q
        .group_by(DimCliente.cliente_nombre)
        .order_by(func.sum(FactSalesInConstrained.unidades_sales_in_constr).desc())
        .all()
    )

    # SKUs con OOS por cliente: distribuir lost sales proporcionalmente
    # segun la demanda unconstrained de cada cliente para ese SKU.
    # FIX BUG-03: Antes se sumaba el total de lost sales a CADA cliente
    # (duplicando/triplicando). Ahora se prorratea por demanda.
    cli_oos = {}
    cli_lost = {}
    if lost_rows:
        for r in lost_rows:
            sku_lost_total = float(r.lost_sales_total or 0)
            if sku_lost_total <= 0:
                continue

            # Obtener demanda unconstrained por cliente para este SKU
            cli_demand = (
                db.query(
                    DimCliente.cliente_nombre.label("cliente"),
                    func.sum(FactSalesInUnconstrained.unidades_sales_in_unc).label("demand"),
                )
                .join(DimCliente, FactSalesInUnconstrained.cliente_id == DimCliente.cliente_id)
                .filter(
                    FactSalesInUnconstrained.sku_id == r.sku_id,
                    FactSalesInUnconstrained.date_id.in_(proj_ids),
                )
                .group_by(DimCliente.cliente_nombre)
                .all()
            )

            total_demand = sum(float(cd.demand or 0) for cd in cli_demand)
            if total_demand <= 0:
                continue

            # Distribuir lost sales proporcionalmente a la demanda de cada cliente
            for cd in cli_demand:
                cli_name = cd.cliente
                cli_demand_val = float(cd.demand or 0)
                if cli_demand_val > 0:
                    cli_oos.setdefault(cli_name, set()).add(r.sku_id)
                    proportion = cli_demand_val / total_demand
                    cli_lost[cli_name] = cli_lost.get(cli_name, 0) + (sku_lost_total * proportion)

    cliente_data = [
        {
            "cliente": r.cliente,
            "ventas_constr_12m": round(float(r.constr or 0)),
            "skus_con_oos": len(cli_oos.get(r.cliente, set())),
            "ventas_perdidas": round(cli_lost.get(r.cliente, 0)),
        }
        for r in constr_by_cli
    ]

    return {
        "kpis": {
            "ventas_constr_12m": round(float(total_si_constr)),
            "ventas_perdidas_oos": round(total_lost),
            "skus_riesgo_oos": skus_oos,
        },
        "by_familia": familia_data,
        "by_cliente": cliente_data,
    }


# ─────────────────────────────────────────────
# CATALOGO
# ─────────────────────────────────────────────


@router.get("/catalog/clients")
def list_clients(db: Session = Depends(get_db)):
    """Lista todos los clientes con sus atributos."""
    rows = db.query(DimCliente).order_by(DimCliente.cliente_nombre).all()
    return [
        {
            "cliente_id": r.cliente_id,
            "cliente_nombre": r.cliente_nombre,
            "canal": r.canal,
            "region": r.region,
            "status": r.status,
            "termino_operaciones": r.termino_operaciones,
        }
        for r in rows
    ]


@router.get("/catalog/skus")
def list_skus(db: Session = Depends(get_db)):
    """Lista todos los SKUs con sus atributos."""
    rows = db.query(DimSku).order_by(DimSku.sku_descripcion).all()
    return [
        {
            "sku_id": r.sku_id,
            "upc": r.upc,
            "sku_descripcion": r.sku_descripcion,
            "familia": r.familia,
            "categoria": r.categoria,
            "tipo": r.tipo,
            "status": r.status,
            "lead_time_meses": r.lead_time_meses or 4,
            "master_pack": r.master_pack,
        }
        for r in rows
    ]


@router.get("/catalog/familias")
def list_familias(db: Session = Depends(get_db)):
    """Lista de familias distintas de SKU."""
    rows = (
        db.query(distinct(DimSku.familia))
        .filter(DimSku.familia.isnot(None))
        .order_by(DimSku.familia)
        .all()
    )
    return [r[0] for r in rows]


class ClientUpdateRequest(BaseModel):
    termino_operaciones: str | None = None


@router.patch("/catalog/clients/{cliente_id}")
def update_client(cliente_id: int, body: ClientUpdateRequest, db: Session = Depends(get_db)):
    """Actualiza termino_operaciones de un cliente."""
    cli = db.get(DimCliente, cliente_id)
    if not cli:
        raise HTTPException(status_code=404, detail=f"Cliente {cliente_id} no encontrado")

    if body.termino_operaciones is not None:
        cli.termino_operaciones = body.termino_operaciones
        cli.fecha_actualizacion = datetime.now()

    db.commit()
    return {"message": "Cliente actualizado", "cliente_id": cliente_id}


class SkuUpdateRequest(BaseModel):
    status: str | None = None
    lead_time_meses: int | None = None


@router.patch("/catalog/skus/{sku_id}")
def update_sku(sku_id: int, body: SkuUpdateRequest, db: Session = Depends(get_db)):
    """Actualiza status y/o lead_time_meses de un SKU."""
    sku = db.get(DimSku, sku_id)
    if not sku:
        raise HTTPException(status_code=404, detail=f"SKU {sku_id} no encontrado")

    if body.status is not None:
        sku.status = body.status
    if body.lead_time_meses is not None:
        sku.lead_time_meses = body.lead_time_meses
    sku.fecha_actualizacion = datetime.now()

    db.commit()
    return {"message": "SKU actualizado", "sku_id": sku_id}


# ─────────────────────────────────────────────
# CONFIGURACION
# ─────────────────────────────────────────────


@router.get("/config/params")
def get_config_params(db: Session = Depends(get_db)):
    """Parametros de configuracion (read-only)."""
    min_year = db.query(func.min(DimTiempo.anio)).scalar()
    max_year = db.query(func.max(DimTiempo.anio)).scalar()

    return {
        "dim_tiempo_rango": {
            "min_year": min_year,
            "max_year": max_year,
        },
        "politica_cobertura_meses": 4,
        "min_months_for_trend": 8,
        "graduation_threshold": 4,
        "dos_target_low": 60,
        "dos_target_high": 90,
        "mes_cierre_activo": 202507,
    }


# ─────────────────────────────────────────────
# CONSTRAINT — REPORTE DE ASIGNACION (GATE BLOQUEANTE)
# ─────────────────────────────────────────────


@router.get("/constraint/allocation-report/{date_id}")
def get_allocation_report(date_id: int, db: Session = Depends(get_db)):
    """
    Retorna el reporte de asignacion de inventario limitado agrupado por SKU,
    con subarrays por cliente y periodos.

    Solo incluye registros de la ventana 1..L (periodos de lead time).
    """
    rows = (
        db.query(RptAsignacionInventario)
        .filter(
            RptAsignacionInventario.date_id.in_(_projection_ids(date_id)),
        )
        .order_by(
            RptAsignacionInventario.sku_id,
            RptAsignacionInventario.cliente_id,
            RptAsignacionInventario.periodo_proyeccion,
        )
        .all()
    )

    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No hay reporte de asignacion para date_id={date_id}. "
            f"Ejecute primero POST /constraint/run/{date_id}.",
        )

    # Verificar si ya esta aprobado
    aprobado = all(r.aprobado for r in rows)

    # Agrupar: SKU → cliente → periodos
    skus_dict: dict[int, dict] = {}
    for r in rows:
        if r.sku_id not in skus_dict:
            sku_obj = r.sku
            skus_dict[r.sku_id] = {
                "sku_id": r.sku_id,
                "upc": int(sku_obj.upc) if sku_obj and sku_obj.upc else None,
                "descripcion": sku_obj.sku_descripcion if sku_obj else None,
                "clientes": {},
            }

        sku_entry = skus_dict[r.sku_id]
        if r.cliente_id not in sku_entry["clientes"]:
            cli_obj = r.cliente
            sku_entry["clientes"][r.cliente_id] = {
                "cliente_id": r.cliente_id,
                "cliente_nombre": cli_obj.cliente_nombre if cli_obj else None,
                "periodos": [],
            }

        sku_entry["clientes"][r.cliente_id]["periodos"].append(
            {
                "periodo": r.periodo_proyeccion,
                "date_id": r.date_id,
                "si_unconstrained": float(r.si_unconstrained or 0),
                "si_constrained_auto": float(r.si_constrained_auto or 0),
                "si_constrained_usuario": (
                    float(r.si_constrained_usuario)
                    if r.si_constrained_usuario is not None
                    else None
                ),
            }
        )

    # Convertir dicts internos de clientes a listas
    skus_list = []
    for sku_data in skus_dict.values():
        sku_data["clientes"] = list(sku_data["clientes"].values())
        skus_list.append(sku_data)

    return {
        "date_id_cierre": date_id,
        "aprobado": aprobado,
        "skus": skus_list,
    }


class AllocModificacion(BaseModel):
    sku_id: int
    cliente_id: int
    date_id: int
    si_constrained_usuario: float


class AllocApproveRequest(BaseModel):
    modificaciones: list[AllocModificacion] = []


@router.post("/constraint/allocation-approve/{date_id}")
def approve_allocation(
    date_id: int,
    body: AllocApproveRequest,
    db: Session = Depends(get_db),
):
    """
    Aprueba el reporte de asignacion de inventario limitado.

    Validacion: Para cada (sku_id, date_id), la suma de si_constrained_usuario
    de todos los clientes NO puede exceder la suma de si_constrained_auto
    (el sistema no permite asignar MAS de lo calculado como disponible).

    Al aprobar:
    1. Actualiza si_constrained_usuario en el reporte.
    2. Sobrescribe fact_sales_in_constrained con valores aprobados.
    3. Ejecuta Fases B y C automaticamente.

    Returns:
        Resumen de la aprobacion + resultado de Fases B y C.
    """
    proj_ids = _projection_ids(date_id)

    # Verificar que existe el reporte
    report_rows = (
        db.query(RptAsignacionInventario)
        .filter(RptAsignacionInventario.date_id.in_(proj_ids))
        .all()
    )
    if not report_rows:
        raise HTTPException(
            status_code=404,
            detail="No hay reporte de asignacion. Ejecute primero Constraint Demand.",
        )

    # Indexar reporte: {(sku_id, cliente_id, date_id): row}
    report_index: dict[tuple[int, int, int], RptAsignacionInventario] = {
        (r.sku_id, r.cliente_id, r.date_id): r for r in report_rows
    }

    # 1. Aplicar modificaciones del usuario
    for mod in body.modificaciones:
        key = (mod.sku_id, mod.cliente_id, mod.date_id)
        if key not in report_index:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Modificacion invalida: sku_id={mod.sku_id}, "
                    f"cliente_id={mod.cliente_id}, date_id={mod.date_id} "
                    f"no existe en el reporte."
                ),
            )
        report_index[key].si_constrained_usuario = float(
            round(mod.si_constrained_usuario, 4)
        )

    # 2. Para registros no modificados, copiar si_constrained_auto
    for r in report_rows:
        if r.si_constrained_usuario is None:
            r.si_constrained_usuario = float(r.si_constrained_auto)

    # 3. Validacion: para cada (sku_id, date_id), SUM(usuario) <= SUM(auto)
    # Agrupar por (sku_id, date_id)
    from collections import defaultdict

    sum_auto: dict[tuple[int, int], float] = defaultdict(float)
    sum_usuario: dict[tuple[int, int], float] = defaultdict(float)

    for r in report_rows:
        grp = (r.sku_id, r.date_id)
        sum_auto[grp] += float(r.si_constrained_auto or 0)
        sum_usuario[grp] += float(r.si_constrained_usuario or 0)

    for grp, total_usuario in sum_usuario.items():
        total_auto = sum_auto[grp]
        if total_usuario > total_auto + 0.01:  # tolerancia de redondeo
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Validacion fallida: sku_id={grp[0]}, date_id={grp[1]}. "
                    f"Suma usuario ({total_usuario:.4f}) excede "
                    f"inventario disponible ({total_auto:.4f})."
                ),
            )

    # 4. Marcar como aprobado
    now = datetime.now()
    for r in report_rows:
        r.aprobado = True
        r.fecha_aprobacion = now

    db.flush()

    # 5. Sobrescribir fact_sales_in_constrained con valores aprobados
    #    Solo periodos del reporte (1..L)
    report_date_ids = list(set(r.date_id for r in report_rows))
    for r in report_rows:
        # Buscar registro en fact_sales_in_constrained
        si_constr = (
            db.query(FactSalesInConstrained)
            .filter(
                FactSalesInConstrained.sku_id == r.sku_id,
                FactSalesInConstrained.cliente_id == r.cliente_id,
                FactSalesInConstrained.date_id == r.date_id,
            )
            .first()
        )
        if si_constr:
            si_constr.unidades_sales_in_constr = float(r.si_constrained_usuario)
        else:
            # No deberia pasar, pero por seguridad insertamos
            db.add(
                FactSalesInConstrained(
                    date_id=r.date_id,
                    cliente_id=r.cliente_id,
                    sku_id=r.sku_id,
                    unidades_sales_in_constr=float(r.si_constrained_usuario),
                )
            )

    db.flush()

    # 6. Ejecutar Fases B y C automaticamente
    from .constraint_demand_phase_b import run_phase_b_and_c

    phase_bc_result = run_phase_b_and_c(db, date_id)

    return {
        "aprobacion": {
            "date_id_cierre": date_id,
            "registros_aprobados": len(report_rows),
            "modificaciones_usuario": len(body.modificaciones),
            "fecha_aprobacion": now.isoformat(),
        },
        "fase_b_c": phase_bc_result,
    }


# ─────────────────────────────────────────────
# REPORTES VENTAS PERDIDAS (FASE C)
# ─────────────────────────────────────────────


@router.get("/reports/lost-sales-so/{date_id}")
def get_lost_sales_so(
    date_id: int,
    level: str = "cliente_sku",
    db: Session = Depends(get_db),
):
    """
    Reporte de ventas perdidas Sales Out por nivel de agregacion.

    Niveles:
    - cliente_sku: detalle por cliente y SKU.
    - cliente_familia: agrupado por cliente + familia de SKU.
    - cliente_total: agrupado por cliente.
    - total_tenka: gran total.

    Excluye filas donde total lost = 0.
    """
    proj_ids = _projection_ids(date_id)

    rows = (
        db.query(
            RptLostSalesSalesOut,
            DimCliente.cliente_nombre,
            DimSku.upc,
            DimSku.sku_descripcion,
            DimSku.familia,
        )
        .join(DimCliente, RptLostSalesSalesOut.cliente_id == DimCliente.cliente_id)
        .join(DimSku, RptLostSalesSalesOut.sku_id == DimSku.sku_id)
        .filter(RptLostSalesSalesOut.date_id.in_(proj_ids))
        .all()
    )

    if level == "cliente_sku":
        data = []
        for r, cli_nombre, upc, sku_desc, familia in rows:
            data.append(
                {
                    "cliente": cli_nombre,
                    "sku_descripcion": sku_desc,
                    "upc": int(upc) if upc else None,
                    "familia": familia,
                    "periodo": r.periodo_proyeccion,
                    "date_id": r.date_id,
                    "so_unconstrained": float(r.so_unconstrained or 0),
                    "so_constrained": float(r.so_constrained or 0),
                    "lost_sales": float(r.lost_sales or 0),
                }
            )
        return {"level": level, "date_id_cierre": date_id, "data": data}

    elif level == "cliente_familia":
        from collections import defaultdict

        groups: dict[tuple[str, str], float] = defaultdict(float)
        for r, cli_nombre, upc, sku_desc, familia in rows:
            groups[(cli_nombre, familia or "Sin Familia")] += float(
                r.lost_sales or 0
            )
        data = [
            {"cliente": k[0], "familia": k[1], "total_lost_sales": round(v, 4)}
            for k, v in groups.items()
            if v > 0
        ]
        return {"level": level, "date_id_cierre": date_id, "data": data}

    elif level == "cliente_total":
        from collections import defaultdict

        groups: dict[str, float] = defaultdict(float)
        for r, cli_nombre, upc, sku_desc, familia in rows:
            groups[cli_nombre] += float(r.lost_sales or 0)
        data = [
            {"cliente": k, "total_lost_sales": round(v, 4)}
            for k, v in groups.items()
            if v > 0
        ]
        return {"level": level, "date_id_cierre": date_id, "data": data}

    elif level == "total_tenka":
        total = sum(float(r.lost_sales or 0) for r, *_ in rows)
        return {
            "level": level,
            "date_id_cierre": date_id,
            "total_lost_sales": round(total, 4),
        }

    else:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Nivel invalido: '{level}'. "
                f"Usar: cliente_sku, cliente_familia, cliente_total, total_tenka."
            ),
        )


@router.get("/reports/lost-sales-si/{date_id}")
def get_lost_sales_si(
    date_id: int,
    level: str = "cliente_sku",
    db: Session = Depends(get_db),
):
    """
    Reporte de ventas perdidas Sales In por nivel de agregacion.

    Misma estructura que lost-sales-so pero con datos de Sales In.
    """
    proj_ids = _projection_ids(date_id)

    rows = (
        db.query(
            RptLostSalesSalesIn,
            DimCliente.cliente_nombre,
            DimSku.upc,
            DimSku.sku_descripcion,
            DimSku.familia,
        )
        .join(DimCliente, RptLostSalesSalesIn.cliente_id == DimCliente.cliente_id)
        .join(DimSku, RptLostSalesSalesIn.sku_id == DimSku.sku_id)
        .filter(RptLostSalesSalesIn.date_id.in_(proj_ids))
        .all()
    )

    if level == "cliente_sku":
        data = []
        for r, cli_nombre, upc, sku_desc, familia in rows:
            data.append(
                {
                    "cliente": cli_nombre,
                    "sku_descripcion": sku_desc,
                    "upc": int(upc) if upc else None,
                    "familia": familia,
                    "periodo": r.periodo_proyeccion,
                    "date_id": r.date_id,
                    "si_unconstrained": float(r.si_unconstrained or 0),
                    "si_constrained": float(r.si_constrained or 0),
                    "lost_sales": float(r.lost_sales or 0),
                }
            )
        return {"level": level, "date_id_cierre": date_id, "data": data}

    elif level == "cliente_familia":
        from collections import defaultdict

        groups: dict[tuple[str, str], float] = defaultdict(float)
        for r, cli_nombre, upc, sku_desc, familia in rows:
            groups[(cli_nombre, familia or "Sin Familia")] += float(
                r.lost_sales or 0
            )
        data = [
            {"cliente": k[0], "familia": k[1], "total_lost_sales": round(v, 4)}
            for k, v in groups.items()
            if v > 0
        ]
        return {"level": level, "date_id_cierre": date_id, "data": data}

    elif level == "cliente_total":
        from collections import defaultdict

        groups: dict[str, float] = defaultdict(float)
        for r, cli_nombre, upc, sku_desc, familia in rows:
            groups[cli_nombre] += float(r.lost_sales or 0)
        data = [
            {"cliente": k, "total_lost_sales": round(v, 4)}
            for k, v in groups.items()
            if v > 0
        ]
        return {"level": level, "date_id_cierre": date_id, "data": data}

    elif level == "total_tenka":
        total = sum(float(r.lost_sales or 0) for r, *_ in rows)
        return {
            "level": level,
            "date_id_cierre": date_id,
            "total_lost_sales": round(total, 4),
        }

    else:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Nivel invalido: '{level}'. "
                f"Usar: cliente_sku, cliente_familia, cliente_total, total_tenka."
            ),
        )
