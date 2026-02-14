"""
api.py — API FastAPI para el sistema de inventarios y ventas.

Endpoints Fase 1:
    POST /upload/sku                          -> Carga catalogo de SKU
    POST /upload/sales-in                     -> Carga Sales In
    POST /upload/sales-out                    -> Carga Sales Out
    POST /upload/stock-consolidated           -> Carga Stock Consolidado
    POST /upload/internal-inventory           -> Carga Inventario Interno
    POST /upload/transit-inventory            -> Carga Inventario en Transito
    POST /upload/forecast                     -> Carga Forecast 2026
    POST /monthly-close/{date_id}             -> Ejecuta cierre mensual (solo rolling)
    POST /init-time-dimension                 -> Inicializa dim_tiempo
    GET  /test/sales-in/{cliente}             -> Consulta Sales In por cliente

Endpoints Fase 2:
    POST /unconstrained/run/{date_id}         -> Proceso mensual completo + Unconstrained
    GET  /unconstrained/summary/{cliente}     -> Resumen Unconstrained por cliente

Endpoints Fase 2b:
    GET  /new-clients/unregistered            -> Lista clientes/SKUs nuevos sin registrar
    POST /new-clients/register                -> Registra pares cliente-SKU como dados de alta
    GET  /report/aggregated/{date_id}         -> Reporte agregado de 4 niveles

Endpoints Fase 3 (Constraint Demand):
    POST /constraint/run/{date_id}            -> Ejecuta Constraint Demand
    GET  /constraint/summary/{sku_id}         -> Resumen constrained por SKU
"""

import os
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .database import SessionLocal, engine, get_db
from .ingestion_jobs import (
    load_forecast_2026,
    load_internal_inventory,
    load_sales_in,
    load_sales_out,
    load_sku_catalog,
    load_stock_consolidated,
    load_transit_inventory,
)
from .constraint_demand import run_constraint_process
from .models import (
    Base,
    DimCliente,
    DimSku,
    FactInventoryInternalConstrained,
    FactInventoryUnconstrained,
    FactPoInterno,
    FactSalesIn,
    FactSalesInConstrained,
    FactSalesInUnconstrained,
    FactSalesOutUnconstrained,
    RptForecastSinCatalogo,
    RptLostSalesOos,
)
from .monthly_close import run_monthly_close
from .monthly_process import run_monthly_process
from .report_generator import generate_aggregated_report
from .time_dimension import ensure_time_dimension

# FIX INF-04: Reemplazado @app.on_event("startup") deprecado con lifespan
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Crea las tablas en la BD si no existen al iniciar."""
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="Tenka Inventory & Sales Control",
    description="API para ingesta de datos y control de inventarios con esquema snowflake.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — permite todas las origenes para staging/demo
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Registrar endpoints del frontend
from .api_frontend import router as frontend_router  # noqa: E402
app.include_router(frontend_router)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────


def _save_temp_file(upload: UploadFile) -> str:
    """Guarda un UploadFile en un archivo temporal y retorna la ruta."""
    suffix = os.path.splitext(upload.filename or ".xlsx")[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = upload.file.read()
        tmp.write(content)
        return tmp.name


def _cleanup(path: str):
    """Elimina un archivo temporal."""
    try:
        os.unlink(path)
    except OSError:
        pass


# ─────────────────────────────────────────────
# Endpoints de inicialización
# ─────────────────────────────────────────────


@app.post("/init-time-dimension")
def init_time_dimension(
    start_year: int = 2023,
    end_year: int = 2027,
    db: Session = Depends(get_db),
):
    """
    Inicializa la dimensión tiempo para el rango de años indicado.
    Por defecto cubre 2023-2027 (histórico + proyectado).
    """
    inserted = ensure_time_dimension(db, start_year, end_year)
    return {"message": f"{inserted} registros insertados en dim_tiempo", "start_year": start_year, "end_year": end_year}


# ─────────────────────────────────────────────
# Endpoints de upload
# ─────────────────────────────────────────────


@app.post("/upload/sku")
def upload_sku(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Carga catálogo de SKU desde un archivo Excel."""
    path = _save_temp_file(file)
    try:
        count = load_sku_catalog(path, session=db)
        return {"message": f"{count} SKUs procesados", "filename": file.filename}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        _cleanup(path)


@app.post("/upload/sales-in")
def upload_sales_in(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Carga datos de Sales In desde un archivo Excel."""
    path = _save_temp_file(file)
    try:
        count = load_sales_in(path, session=db)
        return {"message": f"{count} registros de Sales In insertados", "filename": file.filename}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        _cleanup(path)


@app.post("/upload/sales-out")
def upload_sales_out(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Carga datos de Sales Out desde un archivo Excel."""
    path = _save_temp_file(file)
    try:
        count = load_sales_out(path, session=db)
        return {"message": f"{count} registros de Sales Out insertados", "filename": file.filename}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        _cleanup(path)


@app.post("/upload/stock-consolidated")
def upload_stock_consolidated(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Carga datos de Stock Consolidado desde un archivo Excel."""
    path = _save_temp_file(file)
    try:
        count = load_stock_consolidated(path, session=db)
        return {"message": f"{count} registros de Stock insertados", "filename": file.filename}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        _cleanup(path)


@app.post("/upload/internal-inventory")
def upload_internal_inventory(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Carga datos de Inventario Interno desde un archivo Excel."""
    path = _save_temp_file(file)
    try:
        count = load_internal_inventory(path, session=db)
        return {"message": f"{count} registros de Inventario Interno insertados", "filename": file.filename}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        _cleanup(path)


@app.post("/upload/transit-inventory")
def upload_transit_inventory(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Carga datos de Inventario en Tránsito desde un archivo Excel."""
    path = _save_temp_file(file)
    try:
        count = load_transit_inventory(path, session=db)
        return {"message": f"{count} registros de Inventario en Tránsito insertados", "filename": file.filename}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        _cleanup(path)


@app.post("/upload/forecast")
def upload_forecast(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Carga datos de Forecast desde un archivo Excel."""
    path = _save_temp_file(file)
    try:
        count = load_forecast_2026(path, session=db)
        return {"message": f"{count} registros de Forecast insertados", "filename": file.filename}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        _cleanup(path)


# ─────────────────────────────────────────────
# Endpoint de cierre mensual
# ─────────────────────────────────────────────


@app.post("/monthly-close/{mes_cierre_date_id}")
def monthly_close(mes_cierre_date_id: int, db: Session = Depends(get_db)):
    """
    Ejecuta el proceso de cierre mensual.
    Pre-condición: los datos del mes ya deben estar cargados.
    """
    try:
        result = run_monthly_close(db, mes_cierre_date_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# Endpoint de consulta
# ─────────────────────────────────────────────


@app.get("/test/sales-in/{cliente_nombre}")
def get_sales_in_by_client(cliente_nombre: str, db: Session = Depends(get_db)):
    """
    Devuelve las unidades de fact_sales_in agrupadas por date_id
    para el cliente indicado.
    """
    cliente = db.query(DimCliente).filter(DimCliente.cliente_nombre == cliente_nombre).first()
    if not cliente:
        raise HTTPException(status_code=404, detail=f"Cliente '{cliente_nombre}' no encontrado")

    results = (
        db.query(
            FactSalesIn.date_id,
            func.sum(FactSalesIn.unidades_sales_in).label("total_unidades"),
        )
        .filter(FactSalesIn.cliente_id == cliente.cliente_id)
        .group_by(FactSalesIn.date_id)
        .order_by(FactSalesIn.date_id)
        .all()
    )

    return {
        "cliente": cliente_nombre,
        "data": [
            {"date_id": row.date_id, "total_unidades": row.total_unidades}
            for row in results
        ],
    }


# ─────────────────────────────────────────────
# Fase 2: Endpoints de Unconstrained Demand
# ─────────────────────────────────────────────


@app.post("/unconstrained/run/{mes_cierre_date_id}")
def run_unconstrained(mes_cierre_date_id: int, db: Session = Depends(get_db)):
    """
    Ejecuta el proceso mensual completo: cierre + Unconstrained Demand.

    Pre-condición: los archivos del mes de cierre ya deben estar cargados
    mediante los endpoints de upload.
    """
    try:
        result = run_monthly_process(db, mes_cierre_date_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/unconstrained/summary/{cliente_nombre}")
def get_unconstrained_summary(cliente_nombre: str, db: Session = Depends(get_db)):
    """
    Devuelve el resumen de Unconstrained Demand para un cliente:
        - 12 meses de Sales Out Unconstrained
        - 12 meses de Sales In Unconstrained
        - 12 meses de Inventario Final Unconstrained
        - 12 meses de Days of Sale Unconstrained
    """
    cliente = db.query(DimCliente).filter(
        DimCliente.cliente_nombre == cliente_nombre
    ).first()
    if not cliente:
        raise HTTPException(
            status_code=404,
            detail=f"Cliente '{cliente_nombre}' no encontrado",
        )

    cid = cliente.cliente_id

    # Sales Out Unconstrained
    so_unc = (
        db.query(
            FactSalesOutUnconstrained.date_id,
            func.sum(FactSalesOutUnconstrained.unidades_sales_out_unc).label("total"),
        )
        .filter(FactSalesOutUnconstrained.cliente_id == cid)
        .group_by(FactSalesOutUnconstrained.date_id)
        .order_by(FactSalesOutUnconstrained.date_id)
        .all()
    )

    # Sales In Unconstrained
    si_unc = (
        db.query(
            FactSalesInUnconstrained.date_id,
            func.sum(FactSalesInUnconstrained.unidades_sales_in_unc).label("total"),
        )
        .filter(FactSalesInUnconstrained.cliente_id == cid)
        .group_by(FactSalesInUnconstrained.date_id)
        .order_by(FactSalesInUnconstrained.date_id)
        .all()
    )

    # Inventario y DOS Unconstrained
    inv_unc = (
        db.query(
            FactInventoryUnconstrained.date_id,
            func.sum(FactInventoryUnconstrained.inventario_final_unc).label("inventario"),
            func.avg(FactInventoryUnconstrained.days_of_sale_unc).label("dos"),
            func.avg(FactInventoryUnconstrained.months_of_sale_unc).label("mos"),
        )
        .filter(FactInventoryUnconstrained.cliente_id == cid)
        .group_by(FactInventoryUnconstrained.date_id)
        .order_by(FactInventoryUnconstrained.date_id)
        .all()
    )

    return {
        "cliente": cliente_nombre,
        "sales_out_unconstrained": [
            {"date_id": r.date_id, "unidades": float(r.total or 0)}
            for r in so_unc
        ],
        "sales_in_unconstrained": [
            {"date_id": r.date_id, "unidades": float(r.total or 0)}
            for r in si_unc
        ],
        "inventory_unconstrained": [
            {
                "date_id": r.date_id,
                "inventario_final": float(r.inventario or 0),
                "days_of_sale": float(r.dos) if r.dos else None,
                "months_of_sale": float(r.mos) if r.mos else None,
            }
            for r in inv_unc
        ],
    }


# ─────────────────────────────────────────────
# Fase 3: Endpoints de nuevos clientes y reporte agregado
# ─────────────────────────────────────────────


class RegisterRequest(BaseModel):
    ids: list[int]


@app.get("/new-clients/unregistered")
def get_unregistered_new_clients(db: Session = Depends(get_db)):
    """Lista todos los clientes/SKUs nuevos sin registrar del forecast."""
    records = (
        db.query(RptForecastSinCatalogo)
        .filter(RptForecastSinCatalogo.registrado == False)
        .all()
    )
    return {
        "count": len(records),
        "records": [
            {
                "id": r.id,
                "cliente_nombre": r.cliente_nombre,
                "upc": r.upc,
                "descripcion": r.descripcion,
                "tipo_problema": r.tipo_problema,
                "comentario": r.comentario,
                "cliente_id": r.cliente_id,
                "sku_id": r.sku_id,
            }
            for r in records
        ],
    }


@app.post("/new-clients/register")
def register_new_clients(request: RegisterRequest, db: Session = Depends(get_db)):
    """
    Marca pares cliente-SKU como registrados (dados de alta).
    Recibe una lista de IDs de rpt_forecast_sin_catalogo.
    """
    updated = 0
    for record_id in request.ids:
        record = db.get(RptForecastSinCatalogo, record_id)
        if record and not record.registrado:
            record.registrado = True
            record.fecha_registro = datetime.now()
            updated += 1

    db.commit()
    return {
        "message": f"{updated} registros marcados como dados de alta.",
        "updated": updated,
    }


@app.get("/report/aggregated/{mes_cierre_date_id}")
def get_aggregated_report(mes_cierre_date_id: int, db: Session = Depends(get_db)):
    """
    Genera y devuelve el reporte agregado de 4 niveles como JSON.
    """
    try:
        df = generate_aggregated_report(db, mes_cierre_date_id)
        if df.empty:
            return {"message": "Sin datos para generar reporte.", "data": []}

        output_cols = [
            "level", "nivel_nombre", "cliente", "familia", "sku_descripcion", "upc",
            "date_id", "tipo", "sales_out", "sales_in", "inventario_final", "days_of_sale",
        ]
        df_out = df[[c for c in output_cols if c in df.columns]]
        records = df_out.where(df_out.notna(), None).to_dict(orient="records")
        return {
            "mes_cierre_date_id": mes_cierre_date_id,
            "total_rows": len(records),
            "data": records,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# Fase 3: Endpoints de Constraint Demand
# ─────────────────────────────────────────────


@app.post("/constraint/run/{mes_cierre_date_id}")
def run_constraint(mes_cierre_date_id: int, db: Session = Depends(get_db)):
    """
    Ejecuta Fase 3: Constraint Demand.

    Pre-condicion: Fase 2 (Unconstrained Demand) ya se ejecuto para este
    mes de cierre y existen registros en fact_sales_in_unconstrained.
    """
    try:
        result = run_constraint_process(db, mes_cierre_date_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/constraint/summary/{sku_id}")
def get_constraint_summary(sku_id: int, db: Session = Depends(get_db)):
    """
    Resumen de Constraint Demand para un SKU:
    - Inventario interno constrained (12 meses)
    - POs generadas (orden y llegada)
    - Sales In Unconstrained vs Constrained por mes
    - Lost sales en ventana de lead time
    """
    sku = db.get(DimSku, sku_id)
    if not sku:
        raise HTTPException(status_code=404, detail=f"SKU {sku_id} no encontrado")

    # Inventario interno constrained
    inv_rows = (
        db.query(
            FactInventoryInternalConstrained.date_id,
            FactInventoryInternalConstrained.inventario_final_int,
        )
        .filter(FactInventoryInternalConstrained.sku_id == sku_id)
        .order_by(FactInventoryInternalConstrained.date_id)
        .all()
    )

    # POs
    po_rows = (
        db.query(FactPoInterno)
        .filter(FactPoInterno.sku_id == sku_id)
        .order_by(FactPoInterno.date_id_orden)
        .all()
    )

    # Sales In Unconstrained vs Constrained (agregados por mes)
    si_unc = (
        db.query(
            FactSalesInUnconstrained.date_id,
            func.sum(FactSalesInUnconstrained.unidades_sales_in_unc).label("total"),
        )
        .filter(FactSalesInUnconstrained.sku_id == sku_id)
        .group_by(FactSalesInUnconstrained.date_id)
        .order_by(FactSalesInUnconstrained.date_id)
        .all()
    )

    si_constr = (
        db.query(
            FactSalesInConstrained.date_id,
            func.sum(FactSalesInConstrained.unidades_sales_in_constr).label("total"),
        )
        .filter(FactSalesInConstrained.sku_id == sku_id)
        .group_by(FactSalesInConstrained.date_id)
        .order_by(FactSalesInConstrained.date_id)
        .all()
    )

    # Lost sales
    lost = (
        db.query(RptLostSalesOos)
        .filter(RptLostSalesOos.sku_id == sku_id)
        .first()
    )

    return {
        "sku_id": sku_id,
        "sku_descripcion": sku.sku_descripcion,
        "upc": sku.upc,
        "lead_time_meses": sku.lead_time_meses or 4,
        "inventario_interno_constrained": [
            {"date_id": r.date_id, "inventario_final_int": float(r.inventario_final_int)}
            for r in inv_rows
        ],
        "pos": [
            {
                "date_id_orden": r.date_id_orden,
                "date_id_llegada": r.date_id_llegada,
                "unidades_po": float(r.unidades_po),
            }
            for r in po_rows
        ],
        "sales_in_comparison": {
            "unconstrained": [
                {"date_id": r.date_id, "unidades": float(r.total or 0)}
                for r in si_unc
            ],
            "constrained": [
                {"date_id": r.date_id, "unidades": float(r.total or 0)}
                for r in si_constr
            ],
        },
        "lost_sales": {
            "total": float(lost.lost_sales_total) if lost else 0,
            "date_id_inicio_lt": lost.date_id_inicio_lt if lost else None,
            "date_id_fin_lt": lost.date_id_fin_lt if lost else None,
            "detalles": lost.detalles if lost else None,
        } if lost else None,
    }


# ─────────────────────────────────────────────
# SPA Frontend: servir archivos estáticos del build de React
# ─────────────────────────────────────────────

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

if STATIC_DIR.is_dir():
    # Servir assets (JS, CSS, imágenes) directamente
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str):
        """Catch-all: sirve index.html para cualquier ruta no-API (SPA routing)."""
        file_path = STATIC_DIR / full_path
        if file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(STATIC_DIR / "index.html")
