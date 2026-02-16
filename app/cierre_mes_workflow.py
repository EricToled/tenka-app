"""
cierre_mes_workflow.py --- Wizard endpoints for the monthly close workflow.

Endpoints (all under /api/cierre):
    GET  /closing-month         -> Detect next closing month (R1)
    POST /upload-sales          -> Paste-upload Sales In / Sales Out (R2)
    POST /calculate-inventory   -> Client inventory + retrospective correction (R3, R4)
    POST /upload-internal-inv   -> Paste-upload internal inventory (R7)
    POST /run-projections       -> Run monthly process + constraint process (R8)

Private helpers:
    _retrospective_correction   -> Backwards inventory recalc (R4)
    _recalculate_dos_for_pair   -> DOS recalc with first_sale_date adjustment (R5)
"""

import io
import logging
from datetime import datetime

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .database import get_db
from .eligibility import _generate_date_id_range
from .ingestion_core import get_or_create_cliente_id, get_date_id_from_date
from .models import (
    DimSku,
    FactDiasInventarioHistorico,
    FactInventarioInterno,
    FactSalesIn,
    FactSalesOut,
    FactStockCliente,
)
from .monthly_close import _compute_month_minus_n, _compute_month_plus_n

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cierre", tags=["Cierre de Mes"])

# Month names for human-readable labels
_MESES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}


# -----------------------------------------------
# 1.1 GET /closing-month  (R1)
# -----------------------------------------------

@router.get("/closing-month")
def get_closing_month(db: Session = Depends(get_db)):
    """
    R1: Detect the next closing month.
    closing_date_id = MAX(fact_stock_cliente.date_id) + 1 month.
    """
    max_date_id = db.query(func.max(FactStockCliente.date_id)).scalar()
    if not max_date_id:
        raise HTTPException(
            status_code=404,
            detail="No hay datos en fact_stock_cliente. Suba datos historicos primero.",
        )

    closing_date_id = _compute_month_plus_n(max_date_id, 1)
    mes = closing_date_id % 100
    anio = closing_date_id // 100
    label = f"{_MESES.get(mes, mes)} {anio}"

    return {
        "date_id": closing_date_id,
        "label": label,
        "last_data_month": max_date_id,
    }


# -----------------------------------------------
# 1.2 POST /upload-sales  (R2)
# -----------------------------------------------

class PasteUploadRequest(BaseModel):
    tsv_data: str
    tipo: str  # "sales_in" or "sales_out"
    mes_cierre_date_id: int


def _parse_tsv_or_csv(raw: str) -> pd.DataFrame:
    """Parse paste data as TSV first, fallback to CSV."""
    if "\t" in raw:
        df = pd.read_csv(io.StringIO(raw), sep="\t")
    else:
        df = pd.read_csv(io.StringIO(raw))
    # Normalize column names
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    return df


_COL_MAP = {
    "fecha_final": "fecha_final",
    "cliente": "cliente",
    "upc": "upc",
    "unidades": "unidades",
}


def _parse_date_flexible(val: str) -> datetime:
    """Try multiple date formats."""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(val).strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"Formato de fecha no reconocido: {val}")


@router.post("/upload-sales")
def upload_sales(req: PasteUploadRequest, db: Session = Depends(get_db)):
    """
    R2: Parse paste TSV/CSV for Sales In or Sales Out.
    Auto-enrich from dim_sku, auto-create clients.
    """
    if req.tipo not in ("sales_in", "sales_out"):
        raise HTTPException(400, detail="tipo debe ser 'sales_in' o 'sales_out'")

    try:
        df = _parse_tsv_or_csv(req.tsv_data)
    except Exception as e:
        raise HTTPException(400, detail=f"Error al parsear datos: {e}")

    # Map columns
    col_rename = {}
    for col in df.columns:
        if col in _COL_MAP:
            col_rename[col] = _COL_MAP[col]
    df = df.rename(columns=col_rename)

    required = ["fecha_final", "cliente", "upc", "unidades"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise HTTPException(
            400,
            detail=f"Columnas faltantes: {missing}. Esperadas: {required}",
        )

    # Build SKU lookup
    sku_lookup: dict[int, dict] = {}
    for row in db.query(DimSku).all():
        sku_lookup[row.upc] = {
            "sku_id": row.sku_id,
            "descripcion": row.sku_descripcion,
            "familia": row.familia,
        }

    errors = []
    inserted = 0
    enriched_preview = []

    for idx, row in df.iterrows():
        try:
            upc = int(float(row["upc"]))
        except (ValueError, TypeError):
            errors.append({"row": idx + 2, "error": f"UPC invalido: {row['upc']}"})
            continue

        if upc not in sku_lookup:
            errors.append({"row": idx + 2, "error": f"UPC {upc} no encontrado en catalogo"})
            continue

        sku_info = sku_lookup[upc]
        sku_id = sku_info["sku_id"]

        cliente_nombre = str(row["cliente"]).strip()
        cliente_id = get_or_create_cliente_id(db, cliente_nombre)

        try:
            fecha = _parse_date_flexible(row["fecha_final"])
            date_id = get_date_id_from_date(db, fecha)
        except (ValueError, Exception) as e:
            errors.append({"row": idx + 2, "error": str(e)})
            continue

        unidades = float(row["unidades"])

        if req.tipo == "sales_in":
            db.add(FactSalesIn(
                date_id=date_id,
                cliente_id=cliente_id,
                sku_id=sku_id,
                unidades_sales_in=round(unidades, 4),
            ))
        else:
            db.add(FactSalesOut(
                date_id=date_id,
                cliente_id=cliente_id,
                sku_id=sku_id,
                unidades_sales_out=round(unidades, 4),
            ))

        inserted += 1
        if len(enriched_preview) < 20:
            enriched_preview.append({
                "fecha_final": str(row["fecha_final"]),
                "cliente": cliente_nombre,
                "upc": upc,
                "descripcion": sku_info["descripcion"],
                "familia": sku_info["familia"],
                "unidades": unidades,
                "date_id": date_id,
            })

    db.commit()
    return {
        "inserted": inserted,
        "errors": errors,
        "enriched_preview": enriched_preview,
    }


# -----------------------------------------------
# 1.3 POST /calculate-inventory  (R3, R4)
# -----------------------------------------------

class CalcInventoryRequest(BaseModel):
    mes_cierre_date_id: int


@router.post("/calculate-inventory")
def calculate_inventory(req: CalcInventoryRequest, db: Session = Depends(get_db)):
    """
    R3: inv_final_cierre = inv_anterior + SI_cierre - SO_cierre
    R4: If negative, truncate to 0 and run retrospective correction.
    """
    mes_cierre = req.mes_cierre_date_id
    mes_anterior = _compute_month_minus_n(mes_cierre, 1)

    # Historical window for retrospective correction
    hist_start = _compute_month_minus_n(mes_cierre, 11)
    hist_ids = _generate_date_id_range(hist_start, mes_cierre)

    # All client-SKU pairs with activity in closing month
    pairs_in = (
        db.query(FactSalesIn.cliente_id, FactSalesIn.sku_id)
        .filter(FactSalesIn.date_id == mes_cierre)
        .distinct().all()
    )
    pairs_out = (
        db.query(FactSalesOut.cliente_id, FactSalesOut.sku_id)
        .filter(FactSalesOut.date_id == mes_cierre)
        .distinct().all()
    )
    all_pairs = set()
    all_pairs.update((r.cliente_id, r.sku_id) for r in pairs_in)
    all_pairs.update((r.cliente_id, r.sku_id) for r in pairs_out)

    if not all_pairs:
        return {"calculated": 0, "corrections": 0, "correction_details": []}

    # Delete existing stock for closing month (allow re-run)
    db.query(FactStockCliente).filter(
        FactStockCliente.date_id == mes_cierre
    ).delete(synchronize_session=False)
    db.flush()

    calculated = 0
    corrections = 0
    correction_details = []

    for cliente_id, sku_id in all_pairs:
        inv_anterior = (
            db.query(FactStockCliente.inventario_final_unidades)
            .filter(
                FactStockCliente.cliente_id == cliente_id,
                FactStockCliente.sku_id == sku_id,
                FactStockCliente.date_id == mes_anterior,
            )
            .scalar()
        ) or 0

        si_cierre = (
            db.query(func.sum(FactSalesIn.unidades_sales_in))
            .filter(
                FactSalesIn.cliente_id == cliente_id,
                FactSalesIn.sku_id == sku_id,
                FactSalesIn.date_id == mes_cierre,
            )
            .scalar()
        ) or 0

        so_cierre = (
            db.query(func.sum(FactSalesOut.unidades_sales_out))
            .filter(
                FactSalesOut.cliente_id == cliente_id,
                FactSalesOut.sku_id == sku_id,
                FactSalesOut.date_id == mes_cierre,
            )
            .scalar()
        ) or 0

        inv_final = float(inv_anterior) + float(si_cierre) - float(so_cierre)

        needs_correction = inv_final < 0
        if needs_correction:
            inv_final = 0.0

        db.add(FactStockCliente(
            date_id=mes_cierre,
            cliente_id=cliente_id,
            sku_id=sku_id,
            inventario_final_unidades=round(inv_final, 4),
        ))
        calculated += 1

        if needs_correction:
            months_corrected = _retrospective_correction(
                db, cliente_id, sku_id, mes_cierre, hist_ids,
            )
            corrections += 1
            correction_details.append({
                "cliente_id": cliente_id,
                "sku_id": sku_id,
                "months_corrected": months_corrected,
            })

    db.commit()
    return {
        "calculated": calculated,
        "corrections": corrections,
        "correction_details": correction_details,
    }


# -----------------------------------------------
# 1.4 _retrospective_correction  (R4)
# -----------------------------------------------

def _retrospective_correction(
    db: Session,
    cliente_id: int,
    sku_id: int,
    mes_cierre: int,
    hist_ids: list[int],
) -> int:
    """
    R4: Backwards inventory recalculation.
    Start from inv_final[cierre] = 0, then:
        inv_final[t-1] = inv_final[t] - SI[t] + SO[t]
    Update fact_stock_cliente for all corrected months.
    Recalculate DOS for this pair afterwards.
    """
    # Load SI and SO by month for this pair
    si_by_month: dict[int, float] = {}
    so_by_month: dict[int, float] = {}

    si_rows = (
        db.query(FactSalesIn.date_id, func.sum(FactSalesIn.unidades_sales_in).label("val"))
        .filter(
            FactSalesIn.cliente_id == cliente_id,
            FactSalesIn.sku_id == sku_id,
            FactSalesIn.date_id.in_(hist_ids),
        )
        .group_by(FactSalesIn.date_id)
        .all()
    )
    for r in si_rows:
        si_by_month[r.date_id] = float(r.val or 0)

    so_rows = (
        db.query(FactSalesOut.date_id, func.sum(FactSalesOut.unidades_sales_out).label("val"))
        .filter(
            FactSalesOut.cliente_id == cliente_id,
            FactSalesOut.sku_id == sku_id,
            FactSalesOut.date_id.in_(hist_ids),
        )
        .group_by(FactSalesOut.date_id)
        .all()
    )
    for r in so_rows:
        so_by_month[r.date_id] = float(r.val or 0)

    # Walk backwards from cierre
    sorted_ids = sorted(hist_ids, reverse=True)
    inv_current = 0.0  # cierre month = 0
    corrected = 0

    for i, did in enumerate(sorted_ids):
        if i == 0:
            # This is the closing month, already set to 0
            inv_value = 0.0
        else:
            # inv_final[t-1] = inv_final[t] - SI[t] + SO[t]
            next_did = sorted_ids[i - 1]
            si_next = si_by_month.get(next_did, 0.0)
            so_next = so_by_month.get(next_did, 0.0)
            inv_current = inv_current - si_next + so_next
            inv_value = inv_current

        # Update existing record or skip if not found
        existing = (
            db.query(FactStockCliente)
            .filter(
                FactStockCliente.cliente_id == cliente_id,
                FactStockCliente.sku_id == sku_id,
                FactStockCliente.date_id == did,
            )
            .first()
        )
        if existing:
            existing.inventario_final_unidades = round(inv_value, 4)
            corrected += 1

    db.flush()

    # Recalculate DOS for all corrected months
    _recalculate_dos_for_pair(db, cliente_id, sku_id, hist_ids)

    return corrected


# -----------------------------------------------
# 1.5 _recalculate_dos_for_pair  (R5)
# -----------------------------------------------

def _recalculate_dos_for_pair(
    db: Session,
    cliente_id: int,
    sku_id: int,
    hist_ids: list[int],
):
    """
    R5: Recalculate DOS for a client-SKU pair across hist_ids.
    Adjusts N by first_sale_date (NOT hardcoded 12).
    """
    # Delete existing DOS records for this pair in the window
    db.query(FactDiasInventarioHistorico).filter(
        FactDiasInventarioHistorico.cliente_id == cliente_id,
        FactDiasInventarioHistorico.sku_id == sku_id,
        FactDiasInventarioHistorico.date_id.in_(hist_ids),
    ).delete(synchronize_session=False)

    # Pre-load SO for extended trailing window (12 months before oldest hist_id)
    oldest = min(hist_ids)
    trailing_start = _compute_month_minus_n(oldest, 12)
    extended_ids = _generate_date_id_range(trailing_start, max(hist_ids))

    so_by_month: dict[int, float] = {}
    so_rows = (
        db.query(FactSalesOut.date_id, func.sum(FactSalesOut.unidades_sales_out).label("val"))
        .filter(
            FactSalesOut.cliente_id == cliente_id,
            FactSalesOut.sku_id == sku_id,
            FactSalesOut.date_id.in_(extended_ids),
        )
        .group_by(FactSalesOut.date_id)
        .all()
    )
    for r in so_rows:
        so_by_month[r.date_id] = float(r.val or 0)

    # Pre-load inventory for hist_ids
    inv_by_month: dict[int, float] = {}
    inv_rows = (
        db.query(FactStockCliente.date_id, FactStockCliente.inventario_final_unidades)
        .filter(
            FactStockCliente.cliente_id == cliente_id,
            FactStockCliente.sku_id == sku_id,
            FactStockCliente.date_id.in_(hist_ids),
        )
        .all()
    )
    for r in inv_rows:
        inv_by_month[r.date_id] = float(r.inventario_final_unidades or 0)

    # Get first_sale_date for this pair (R5)
    first_sale = (
        db.query(func.min(FactSalesOut.date_id))
        .filter(
            FactSalesOut.cliente_id == cliente_id,
            FactSalesOut.sku_id == sku_id,
        )
        .scalar()
    )

    for did in sorted(hist_ids):
        inv = inv_by_month.get(did, 0.0)

        # Trailing 12 months BEFORE current month
        prev_start = _compute_month_minus_n(did, 12)
        prev_end = _compute_month_minus_n(did, 1)
        prev_ids = _generate_date_id_range(prev_start, prev_end)

        # R5: Adjust by first_sale_date
        if first_sale and first_sale > prev_start:
            prev_ids = [d for d in prev_ids if d >= first_sale]

        n_months = len(prev_ids)
        if n_months == 0:
            avg_so = 0.0
        else:
            total_so = sum(so_by_month.get(d, 0.0) for d in prev_ids)
            avg_so = total_so / n_months

        if avg_so > 0:
            mos = inv / avg_so
            dos = round(mos * 30, 4)
        else:
            mos = None
            dos = None

        db.add(FactDiasInventarioHistorico(
            date_id=did,
            cliente_id=cliente_id,
            sku_id=sku_id,
            inventario_final=round(inv, 4),
            promedio_ventas_12m=round(avg_so, 4) if avg_so > 0 else None,
            months_of_sale_historico=round(mos, 4) if mos is not None else None,
            days_of_sale_historico=dos,
        ))

    db.flush()


# -----------------------------------------------
# 1.6 POST /upload-internal-inv  (R7)
# -----------------------------------------------

class InternalInvUploadRequest(BaseModel):
    tsv_data: str
    mes_cierre_date_id: int


@router.post("/upload-internal-inv")
def upload_internal_inv(req: InternalInvUploadRequest, db: Session = Depends(get_db)):
    """
    R7: Paste-upload internal inventory.
    Headers: UPC, Inventario Cierre de Mes
    """
    try:
        df = _parse_tsv_or_csv(req.tsv_data)
    except Exception as e:
        raise HTTPException(400, detail=f"Error al parsear datos: {e}")

    # Normalize column names for flexibility
    col_rename = {}
    for col in df.columns:
        cl = col.lower().replace(" ", "_")
        if "upc" in cl:
            col_rename[col] = "upc"
        elif "inventario" in cl or "cierre" in cl:
            col_rename[col] = "inventario"
    df = df.rename(columns=col_rename)

    if "upc" not in df.columns or "inventario" not in df.columns:
        raise HTTPException(
            400,
            detail="Columnas requeridas: UPC, Inventario Cierre de Mes",
        )

    # Build SKU lookup
    sku_lookup: dict[int, int] = {}
    for row in db.query(DimSku.sku_id, DimSku.upc).all():
        sku_lookup[row.upc] = row.sku_id

    # Delete existing for this month (allow re-run)
    db.query(FactInventarioInterno).filter(
        FactInventarioInterno.date_id == req.mes_cierre_date_id,
    ).delete(synchronize_session=False)
    db.flush()

    errors = []
    inserted = 0

    for idx, row in df.iterrows():
        try:
            upc = int(float(row["upc"]))
        except (ValueError, TypeError):
            errors.append({"row": idx + 2, "error": f"UPC invalido: {row['upc']}"})
            continue

        if upc not in sku_lookup:
            errors.append({"row": idx + 2, "error": f"UPC {upc} no encontrado en catalogo"})
            continue

        sku_id = sku_lookup[upc]
        inventario = float(row["inventario"])

        db.add(FactInventarioInterno(
            date_id=req.mes_cierre_date_id,
            sku_id=sku_id,
            inventario_interno=round(inventario, 4),
        ))
        inserted += 1

    db.commit()
    return {"inserted": inserted, "errors": errors}


# -----------------------------------------------
# 1.7 POST /run-projections  (R8)
# -----------------------------------------------

class RunProjectionsRequest(BaseModel):
    mes_cierre_date_id: int


@router.post("/run-projections")
def run_projections(req: RunProjectionsRequest, db: Session = Depends(get_db)):
    """
    R8: Run monthly process + constraint process.
    """
    from .monthly_process import run_monthly_process
    from .constraint_demand import run_constraint_process

    try:
        result1 = run_monthly_process(db, req.mes_cierre_date_id)
        result2 = run_constraint_process(db, req.mes_cierre_date_id)
        db.commit()
        return {
            "monthly_process": result1,
            "constraint_process": result2,
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(500, detail=str(e))
