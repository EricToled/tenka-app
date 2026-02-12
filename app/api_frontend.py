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
    from .constraint_demand import _compute_month_plus_n
    start = _compute_month_plus_n(mes_cierre, 1)
    end = _compute_month_plus_n(mes_cierre, 12)
    return _generate_date_id_range(start, end)


# ─────────────────────────────────────────────
# REPORTES — Unconstrained Summary
# ─────────────────────────────────────────────


@router.get("/reports/unconstrained-summary/{date_id}")
def unconstrained_summary(date_id: int, db: Session = Depends(get_db)):
    """
    KPIs + tablas por familia y por cliente para Unconstrained Demand.
    """
    proj_ids = _projection_ids(date_id)

    # ── KPIs ──

    # Total ventas SI unconstrained 12M
    total_si_unc = (
        db.query(func.sum(FactSalesInUnconstrained.unidades_sales_in_unc))
        .filter(FactSalesInUnconstrained.date_id.in_(proj_ids))
        .scalar()
    ) or 0

    # DOS promedio ponderado (inventario / ventas) en ultimo mes
    last_proj = proj_ids[-1] if proj_ids else None
    avg_dos = None
    if last_proj:
        avg_dos_row = (
            db.query(
                func.sum(FactInventoryUnconstrained.inventario_final_unc).label("inv"),
                func.sum(FactSalesOutUnconstrained.unidades_sales_out_unc).label("so"),
            )
            .select_from(FactInventoryUnconstrained)
            .outerjoin(
                FactSalesOutUnconstrained,
                and_(
                    FactSalesOutUnconstrained.date_id == FactInventoryUnconstrained.date_id,
                    FactSalesOutUnconstrained.cliente_id == FactInventoryUnconstrained.cliente_id,
                    FactSalesOutUnconstrained.sku_id == FactInventoryUnconstrained.sku_id,
                ),
            )
            .filter(FactInventoryUnconstrained.date_id == last_proj)
            .first()
        )
        if avg_dos_row and avg_dos_row.inv and avg_dos_row.so:
            # Usar promedio simple de DOS por par
            avg_dos_val = (
                db.query(func.avg(FactInventoryUnconstrained.days_of_sale_unc))
                .filter(
                    FactInventoryUnconstrained.date_id == last_proj,
                    FactInventoryUnconstrained.days_of_sale_unc.isnot(None),
                )
                .scalar()
            )
            avg_dos = round(float(avg_dos_val), 1) if avg_dos_val else None

    # Clientes en riesgo de sobreinventario (MOS > 3 en ultimo mes)
    riesgo_count = 0
    if last_proj:
        riesgo_rows = (
            db.query(FactInventoryUnconstrained.cliente_id)
            .filter(
                FactInventoryUnconstrained.date_id == last_proj,
                FactInventoryUnconstrained.months_of_sale_unc > 3,
            )
            .distinct()
            .all()
        )
        riesgo_count = len(riesgo_rows)

    # ── By Familia ──
    by_familia = (
        db.query(
            DimSku.familia.label("familia"),
            func.sum(FactSalesInUnconstrained.unidades_sales_in_unc).label("ventas_unc_12m"),
        )
        .join(DimSku, FactSalesInUnconstrained.sku_id == DimSku.sku_id)
        .filter(FactSalesInUnconstrained.date_id.in_(proj_ids))
        .group_by(DimSku.familia)
        .order_by(func.sum(FactSalesInUnconstrained.unidades_sales_in_unc).desc())
        .all()
    )

    # Calcular inv objetivo por familia (ultimo mes)
    familia_inv = {}
    familia_so = {}
    if last_proj:
        inv_by_fam = (
            db.query(
                DimSku.familia.label("familia"),
                func.sum(FactInventoryUnconstrained.inventario_final_unc).label("inv"),
            )
            .join(DimSku, FactInventoryUnconstrained.sku_id == DimSku.sku_id)
            .filter(FactInventoryUnconstrained.date_id == last_proj)
            .group_by(DimSku.familia)
            .all()
        )
        familia_inv = {r.familia: float(r.inv or 0) for r in inv_by_fam}

        so_by_fam = (
            db.query(
                DimSku.familia.label("familia"),
                func.sum(FactSalesOutUnconstrained.unidades_sales_out_unc).label("so"),
            )
            .join(DimSku, FactSalesOutUnconstrained.sku_id == DimSku.sku_id)
            .filter(FactSalesOutUnconstrained.date_id.in_(proj_ids))
            .group_by(DimSku.familia)
            .all()
        )
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
    by_cliente = (
        db.query(
            DimCliente.cliente_nombre.label("cliente"),
            func.sum(FactSalesInUnconstrained.unidades_sales_in_unc).label("ventas_unc_12m"),
            func.count(distinct(FactSalesInUnconstrained.sku_id)).label("skus_activos"),
        )
        .join(DimCliente, FactSalesInUnconstrained.cliente_id == DimCliente.cliente_id)
        .filter(FactSalesInUnconstrained.date_id.in_(proj_ids))
        .group_by(DimCliente.cliente_nombre)
        .order_by(func.sum(FactSalesInUnconstrained.unidades_sales_in_unc).desc())
        .all()
    )

    # DOS promedio por cliente en ultimo mes
    cliente_dos = {}
    if last_proj:
        dos_by_cli = (
            db.query(
                DimCliente.cliente_nombre.label("cliente"),
                func.avg(FactInventoryUnconstrained.days_of_sale_unc).label("dos"),
            )
            .join(DimCliente, FactInventoryUnconstrained.cliente_id == DimCliente.cliente_id)
            .filter(
                FactInventoryUnconstrained.date_id == last_proj,
                FactInventoryUnconstrained.days_of_sale_unc.isnot(None),
            )
            .group_by(DimCliente.cliente_nombre)
            .all()
        )
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
def constrained_summary(date_id: int, db: Session = Depends(get_db)):
    """
    KPIs + tablas por familia y por cliente para Constrained Demand.
    """
    proj_ids = _projection_ids(date_id)

    # ── KPIs ──

    total_si_constr = (
        db.query(func.sum(FactSalesInConstrained.unidades_sales_in_constr))
        .filter(FactSalesInConstrained.date_id.in_(proj_ids))
        .scalar()
    ) or 0

    total_si_unc = (
        db.query(func.sum(FactSalesInUnconstrained.unidades_sales_in_unc))
        .filter(FactSalesInUnconstrained.date_id.in_(proj_ids))
        .scalar()
    ) or 0

    lost_rows = db.query(RptLostSalesOos).all()
    total_lost = sum(float(r.lost_sales_total or 0) for r in lost_rows)
    skus_oos = len(lost_rows)

    # ── By Familia ──

    # Unconstrained por familia
    unc_by_fam = (
        db.query(
            DimSku.familia.label("familia"),
            func.sum(FactSalesInUnconstrained.unidades_sales_in_unc).label("unc"),
        )
        .join(DimSku, FactSalesInUnconstrained.sku_id == DimSku.sku_id)
        .filter(FactSalesInUnconstrained.date_id.in_(proj_ids))
        .group_by(DimSku.familia)
        .all()
    )
    unc_fam_map = {r.familia: float(r.unc or 0) for r in unc_by_fam}

    # Constrained por familia
    constr_by_fam = (
        db.query(
            DimSku.familia.label("familia"),
            func.sum(FactSalesInConstrained.unidades_sales_in_constr).label("constr"),
        )
        .join(DimSku, FactSalesInConstrained.sku_id == DimSku.sku_id)
        .filter(FactSalesInConstrained.date_id.in_(proj_ids))
        .group_by(DimSku.familia)
        .all()
    )
    constr_fam_map = {r.familia: float(r.constr or 0) for r in constr_by_fam}

    # Lost sales por familia (via join con dim_sku)
    lost_by_fam = (
        db.query(
            DimSku.familia.label("familia"),
            func.sum(RptLostSalesOos.lost_sales_total).label("lost"),
        )
        .join(DimSku, RptLostSalesOos.sku_id == DimSku.sku_id)
        .group_by(DimSku.familia)
        .all()
    )
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

    constr_by_cli = (
        db.query(
            DimCliente.cliente_nombre.label("cliente"),
            func.sum(FactSalesInConstrained.unidades_sales_in_constr).label("constr"),
        )
        .join(DimCliente, FactSalesInConstrained.cliente_id == DimCliente.cliente_id)
        .filter(FactSalesInConstrained.date_id.in_(proj_ids))
        .group_by(DimCliente.cliente_nombre)
        .order_by(func.sum(FactSalesInConstrained.unidades_sales_in_constr).desc())
        .all()
    )

    # SKUs con OOS por cliente: unir lost_sales con fact_sales_in_constrained
    # Un SKU tiene OOS si tiene lost_sales_total > 0
    oos_sku_ids = {r.sku_id for r in lost_rows}

    # Para cada cliente, contar cuantos de sus SKUs tienen OOS
    cli_oos = {}
    cli_lost = {}
    if oos_sku_ids:
        for r in lost_rows:
            # Buscar clientes que tienen SI para este SKU
            cli_for_sku = (
                db.query(distinct(FactSalesInConstrained.cliente_id))
                .filter(
                    FactSalesInConstrained.sku_id == r.sku_id,
                    FactSalesInConstrained.date_id.in_(proj_ids),
                )
                .all()
            )
            # Distribuir lost sales proporcionalmente por cliente?
            # Simplificamos: contar el SKU como OOS para todos los clientes que lo tienen
            for (cid,) in cli_for_sku:
                cli_name = db.query(DimCliente.cliente_nombre).filter(DimCliente.cliente_id == cid).scalar()
                if cli_name:
                    cli_oos.setdefault(cli_name, set()).add(r.sku_id)
                    cli_lost[cli_name] = cli_lost.get(cli_name, 0) + float(r.lost_sales_total or 0)

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
