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
    FactSalesOutUnconstrained,
    RptLostSalesOos,
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

    # DOS promedio ponderado (inventario / ventas) en ultimo mes
    last_proj = proj_ids[-1] if proj_ids else None
    avg_dos = None
    if last_proj:
        dos_q = (
            db.query(func.avg(FactInventoryUnconstrained.days_of_sale_unc))
            .filter(
                FactInventoryUnconstrained.date_id == last_proj,
                FactInventoryUnconstrained.days_of_sale_unc.isnot(None),
            )
        )
        dos_q = _apply_inv_unc_filters(dos_q)
        avg_dos_val = dos_q.scalar()
        avg_dos = round(float(avg_dos_val), 1) if avg_dos_val else None

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

    # DOS promedio por cliente en ultimo mes
    cliente_dos = {}
    if last_proj:
        dos_cli_q = (
            db.query(
                DimCliente.cliente_nombre.label("cliente"),
                func.avg(FactInventoryUnconstrained.days_of_sale_unc).label("dos"),
            )
            .join(DimCliente, FactInventoryUnconstrained.cliente_id == DimCliente.cliente_id)
            .filter(
                FactInventoryUnconstrained.date_id == last_proj,
                FactInventoryUnconstrained.days_of_sale_unc.isnot(None),
            )
        )
        dos_cli_q = _apply_inv_unc_filters(dos_cli_q)
        dos_by_cli = dos_cli_q.group_by(DimCliente.cliente_nombre).all()
        cliente_dos = {r.cliente: float(r.dos) for r in dos_by_cli}

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
