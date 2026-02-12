"""
run_full_pipeline.py — Ejecuta el pipeline completo: ingesta + Unconstrained Demand.

Usa PostgreSQL local (tenka_db).
Genera un reporte CSV agregado de 4 niveles con 24 periodos (12 hist + 12 proy).
"""

import os
import sys
import logging
from datetime import datetime

# Configurar logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("pipeline")

# Asegurarse de que el proyecto está en el path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# Configurar variables de entorno para la BD antes de importar
os.environ.setdefault("DB_USER", "postgres")
os.environ.setdefault("DB_PASSWORD", "postgres")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "5432")
os.environ.setdefault("DB_NAME", "tenka_db")

# Ahora importar modulos de la app (usaran las env vars)
from app.database import engine, SessionLocal
from app.models import Base
from app.time_dimension import ensure_time_dimension
from app.ingestion_jobs import (
    load_sku_catalog,
    load_sales_in,
    load_sales_out,
    load_stock_consolidated,
    load_internal_inventory,
    load_transit_inventory,
    load_forecast_2026,
)
from app.eligibility import get_eligible_client_skus
from app.historical_dos import build_historical_dos
from app.unconstrained_demand import (
    project_sales_out_unconstrained,
    project_sales_in_and_inventory_unc,
)
from app.new_client_detection import (
    detect_and_report_new_client_skus,
    project_new_client_skus,
)
from app.report_generator import generate_aggregated_report
from app.constraint_demand import run_constraint_process
from app.monthly_close import _compute_month_minus_n
from app.models import (
    DimCliente, DimSku, DimTiempo,
    FactSalesOut, FactSalesIn, FactStockCliente,
    FactSalesOutUnconstrained, FactSalesInUnconstrained,
    FactInventoryUnconstrained, FactDiasInventarioHistorico,
    FactPoInterno, FactSalesInConstrained, FactInventoryInternalConstrained,
    RptInventarioObsoletoCliente, RptForecastSinCatalogo, RptLostSalesOos,
)
from sqlalchemy import func, distinct, text

import pandas as pd


def main():
    DATA_DIR = BASE_DIR

    # ── 1. Verificar conexion ──
    logger.info("=" * 60)
    logger.info("PASO 1: Verificando conexion a PostgreSQL...")
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            logger.info("  Conexion OK a tenka_db")
    except Exception as e:
        logger.error(f"  No se pudo conectar a PostgreSQL: {e}")
        return

    session = SessionLocal()

    try:
        # ── 2. Dimension tiempo ──
        logger.info("=" * 60)
        logger.info("PASO 2: Inicializando dim_tiempo (2022-2027)...")
        n = ensure_time_dimension(session, 2022, 2027)
        logger.info(f"  {n} registros insertados en dim_tiempo.")
        session.commit()

        # ── 3. Ingesta de archivos ──
        logger.info("=" * 60)
        logger.info("PASO 3: Ingesta de archivos Excel...")

        files = {
            "SKU Catalog": ("SKU Control Table.xlsx", load_sku_catalog),
            "Sales In": ("Sales In Table.xlsx", load_sales_in),
            "Sales Out": ("Sales Out Table.xlsx", load_sales_out),
            "Stock Consolidated": ("Sales Stock Consolidated Table.xlsx", load_stock_consolidated),
            "Internal Inventory": ("Tabla inventario Interno.xlsx", load_internal_inventory),
            "Transit Inventory": ("Tabla inventartio en transito.xlsx", load_transit_inventory),
            "Forecast 2026": ("Annual sales Forecast 2026.xlsx", load_forecast_2026),
        }

        for name, (filename, loader) in files.items():
            path = os.path.join(DATA_DIR, filename)
            if not os.path.exists(path):
                logger.warning(f"  SKIP: {filename} no encontrado")
                continue
            try:
                count = loader(path, session=session)
                logger.info(f"  {name}: {count} registros")
            except Exception as e:
                logger.error(f"  ERROR en {name}: {e}")
                import traceback
                traceback.print_exc()
                session.rollback()

        session.commit()

        # ── 4. Diagnostico post-ingesta ──
        logger.info("=" * 60)
        logger.info("PASO 4: Diagnostico post-ingesta...")

        n_sku = session.query(func.count(DimSku.sku_id)).scalar()
        n_clientes = session.query(func.count(DimCliente.cliente_id)).scalar()
        n_tiempo = session.query(func.count(DimTiempo.date_id)).scalar()
        logger.info(f"  dim_sku: {n_sku} registros")
        logger.info(f"  dim_cliente: {n_clientes} registros")
        logger.info(f"  dim_tiempo: {n_tiempo} registros")

        for table_name, model, col in [
            ("fact_sales_in", FactSalesIn, FactSalesIn.date_id),
            ("fact_sales_out", FactSalesOut, FactSalesOut.date_id),
            ("fact_stock_cliente", FactStockCliente, FactStockCliente.date_id),
        ]:
            dates = session.query(distinct(col)).order_by(col).all()
            date_list = [d[0] for d in dates]
            total = session.query(func.count(model.date_id)).scalar()
            logger.info(f"  {table_name}: {total} rows, date_ids={date_list}")

        # ── 5. Determinar ventana real ──
        max_date = session.query(func.max(FactStockCliente.date_id)).scalar()
        min_date = session.query(func.min(FactStockCliente.date_id)).scalar()
        logger.info(f"  fact_stock_cliente rango: {min_date} - {max_date}")

        if max_date is None:
            logger.error("No hay datos en fact_stock_cliente. Abortando.")
            return

        mes_cierre = max_date
        logger.info(f"  Usando mes_cierre = {mes_cierre}")

        # ── 5. Construir DOS historicos ──
        logger.info("=" * 60)
        logger.info(f"PASO 5: Construyendo DOS historicos para cierre {mes_cierre}...")
        dos_count = build_historical_dos(session, mes_cierre)
        logger.info(f"  {dos_count} registros de DOS historico insertados.")
        session.commit()

        # ── 6. Elegibilidad ──
        logger.info("=" * 60)
        logger.info("PASO 6: Determinando elegibilidad cliente-SKU...")
        eligibility = get_eligible_client_skus(session, mes_cierre)
        logger.info(f"  Elegibles: {len(eligibility.eligible)}")
        logger.info(f"  Graduating: {len(eligibility.graduating)}")
        logger.info(f"  Obsoletos: {eligibility.obsolete_count}")
        logger.info(f"  Excluidos: {eligibility.excluded_count}")
        session.commit()

        # ── 6.5. Detectar clientes/SKUs nuevos en forecast ──
        logger.info("=" * 60)
        logger.info("PASO 6.5: Detectando clientes/SKUs nuevos en forecast...")
        new_detection = detect_and_report_new_client_skus(session, mes_cierre)
        logger.info(f"  Clientes nuevos: {len(new_detection['new_clients'])}")
        logger.info(f"  SKUs nuevos: {len(new_detection['new_skus'])}")
        logger.info(f"  SKUs baja: {len(new_detection['baja_skus'])}")
        if new_detection["new_clients"]:
            for nc in new_detection["new_clients"]:
                logger.info(f"    NUEVO: {nc['cliente_nombre']} / UPC {nc['upc']}")
        session.commit()

        has_projections = True
        if not eligibility.eligible:
            logger.warning("No hay cliente-SKU elegibles para proyeccion estadistica.")
            has_projections = False
        else:
            # ── 7. Proyectar Sales Out Unconstrained ──
            logger.info("=" * 60)
            logger.info("PASO 7: Proyectando Sales Out Unconstrained...")
            so_count = project_sales_out_unconstrained(
                session, eligibility.eligible, mes_cierre
            )
            logger.info(f"  {so_count} registros de Sales Out Unc insertados.")
            session.commit()

            # ── 8. Proyectar Sales In + Inventario ──
            logger.info("=" * 60)
            logger.info("PASO 8: Proyectando Sales In, Inventario y DOS Unconstrained...")
            result = project_sales_in_and_inventory_unc(
                session, eligibility.eligible, mes_cierre
            )
            logger.info(f"  Sales In Unc: {result['sales_in_count']} registros")
            logger.info(f"  Inventory Unc: {result['inventory_count']} registros")
            session.commit()

        # ── 8.5. Proyectar clientes nuevos registrados ──
        logger.info("=" * 60)
        logger.info("PASO 8.5: Proyectando clientes nuevos registrados...")
        new_proj = project_new_client_skus(session, mes_cierre)
        logger.info(f"  {new_proj} pares cliente-SKU nuevos proyectados desde forecast.")
        session.commit()

        # ── 9. FASE 3: Constraint Demand ──
        logger.info("=" * 60)
        logger.info("PASO 9: Ejecutando Fase 3 — Constraint Demand...")
        if has_projections or new_proj > 0:
            constraint_result = run_constraint_process(session, mes_cierre)
            logger.info(f"  Estado: {constraint_result['estado']}")
            logger.info(f"  {constraint_result['mensaje']}")
            _print_constraint_summary(session, mes_cierre)
        else:
            logger.warning("  Sin proyecciones Unconstrained, se omite Constraint Demand.")

        # ── 10. Generar reporte agregado de 4 niveles ──
        logger.info("=" * 60)
        logger.info("PASO 10: Generando reporte agregado de 4 niveles...")
        _generate_report(session, mes_cierre, has_projections=has_projections or new_proj > 0)

    except Exception as e:
        session.rollback()
        logger.exception(f"Error fatal: {e}")
        raise
    finally:
        session.close()


def _print_constraint_summary(session, mes_cierre: int):
    """Imprime resumen de Constraint Demand en el log."""
    # POs generadas
    po_count = session.query(func.count(FactPoInterno.fact_po_id)).scalar() or 0
    po_total_units = session.query(func.sum(FactPoInterno.unidades_po)).scalar() or 0
    logger.info(f"  POs generadas: {po_count} (total unidades: {float(po_total_units):,.0f})")

    # Top 5 POs por unidades
    top_pos = (
        session.query(
            DimSku.sku_descripcion,
            FactPoInterno.date_id_orden,
            FactPoInterno.date_id_llegada,
            FactPoInterno.unidades_po,
        )
        .join(DimSku, FactPoInterno.sku_id == DimSku.sku_id)
        .order_by(FactPoInterno.unidades_po.desc())
        .limit(5)
        .all()
    )
    if top_pos:
        logger.info("  Top 5 POs por unidades:")
        for po in top_pos:
            logger.info(
                f"    {po.sku_descripcion[:40]:<40} | "
                f"Orden: {po.date_id_orden} | Llegada: {po.date_id_llegada} | "
                f"Units: {float(po.unidades_po):>10,.0f}"
            )

    # Lost sales
    lost_rows = (
        session.query(
            DimSku.sku_descripcion,
            DimSku.upc,
            RptLostSalesOos.lost_sales_total,
            RptLostSalesOos.date_id_inicio_lt,
            RptLostSalesOos.date_id_fin_lt,
        )
        .join(DimSku, RptLostSalesOos.sku_id == DimSku.sku_id)
        .order_by(RptLostSalesOos.lost_sales_total.desc())
        .all()
    )
    if lost_rows:
        total_lost = sum(float(r.lost_sales_total) for r in lost_rows)
        logger.info(f"  SKUs con OOS / Lost Sales: {len(lost_rows)} (total: {total_lost:,.0f} unidades)")
        for ls in lost_rows[:10]:
            logger.info(
                f"    UPC {ls.upc} | {ls.sku_descripcion[:40]:<40} | "
                f"Lost: {float(ls.lost_sales_total):>10,.0f} | "
                f"Ventana: {ls.date_id_inicio_lt}-{ls.date_id_fin_lt}"
            )
    else:
        logger.info("  Sin lost sales por OOS en ventana de lead time.")


def _generate_report(session, mes_cierre: int, has_projections: bool = True):
    """
    Genera un CSV con reporte de 4 niveles:
    CLIENTE_SKU, CLIENTE_FAMILIA, CLIENTE_TOTAL, TOTAL_TENKA
    24 periodos (12 historicos + 12 proyectados)
    Variables: Sales Out, Sales In, Inventario Final, DOS (recalculado por nivel)
    """
    df = generate_aggregated_report(session, mes_cierre, has_projections)

    if df.empty:
        logger.warning("No hay datos para el reporte.")
        return

    # Guardar CSV (sin columnas internas _cliente_id, _sku_id)
    output_cols = [
        "level", "nivel_nombre", "cliente", "familia", "sku_descripcion", "upc",
        "date_id", "tipo", "sales_out", "sales_in", "inventario_final", "days_of_sale",
    ]
    df_out = df[[c for c in output_cols if c in df.columns]]
    output_path = os.path.join(BASE_DIR, "reporte_unconstrained_demand.csv")
    df_out.to_csv(output_path, index=False, encoding="utf-8-sig")
    logger.info(f"Reporte guardado en: {output_path}")

    # Resumen en consola por nivel
    logger.info("=" * 60)
    logger.info("RESUMEN DEL REPORTE:")
    logger.info("=" * 60)

    for level in sorted(df["level"].unique()):
        level_df = df[df["level"] == level]
        nivel = level_df["nivel_nombre"].iloc[0]
        n_rows = len(level_df)
        n_hist = len(level_df[level_df["tipo"] == "HISTORICO"])
        n_proj = len(level_df[level_df["tipo"] == "PROYECTADO"])
        logger.info(f"  Nivel {level} ({nivel}): {n_rows} filas ({n_hist} hist + {n_proj} proy)")

    # Resumen Nivel 3 (CLIENTE_TOTAL) — resumen ejecutivo
    df3 = df[df["level"] == 3]
    if not df3.empty:
        logger.info(f"\n{'=' * 60}")
        logger.info("RESUMEN POR CLIENTE (NIVEL 3 - CLIENTE_TOTAL):")
        logger.info("=" * 60)

        for cliente in sorted(df3["cliente"].unique()):
            cdf = df3[df3["cliente"] == cliente]
            hist = cdf[cdf["tipo"] == "HISTORICO"]
            proj = cdf[cdf["tipo"] == "PROYECTADO"]
            logger.info(f"\n{'─' * 50}")
            logger.info(f"CLIENTE: {cliente}")
            logger.info(f"{'─' * 50}")

            if not hist.empty:
                logger.info("  HISTORICO (12 meses):")
                for _, r in hist.iterrows():
                    dos_str = f"{r['days_of_sale']:.0f}" if pd.notna(r['days_of_sale']) else "N/A"
                    logger.info(
                        f"    {int(r['date_id'])} | SO: {r['sales_out']:>10.0f} | "
                        f"SI: {r['sales_in']:>10.0f} | "
                        f"Inv: {r['inventario_final']:>10.0f} | "
                        f"DOS: {dos_str:>6}"
                    )

            if not proj.empty:
                logger.info("  PROYECTADO (12 meses):")
                for _, r in proj.iterrows():
                    dos_str = f"{r['days_of_sale']:.0f}" if pd.notna(r['days_of_sale']) else "N/A"
                    logger.info(
                        f"    {int(r['date_id'])} | SO: {r['sales_out']:>10.0f} | "
                        f"SI: {r['sales_in']:>10.0f} | "
                        f"Inv: {r['inventario_final']:>10.0f} | "
                        f"DOS: {dos_str:>6}"
                    )

    # Obsoletos
    obsoletos = session.query(
        DimCliente.cliente_nombre,
        DimSku.upc,
        DimSku.sku_descripcion,
        RptInventarioObsoletoCliente.inventario_final_unidades,
    ).join(
        DimCliente, RptInventarioObsoletoCliente.cliente_id == DimCliente.cliente_id
    ).join(
        DimSku, RptInventarioObsoletoCliente.sku_id == DimSku.sku_id
    ).all()

    if obsoletos:
        logger.info(f"\n{'=' * 60}")
        logger.info(f"INVENTARIO OBSOLETO: {len(obsoletos)} registros")
        for o in obsoletos[:20]:
            logger.info(f"  {o.cliente_nombre} | UPC {o.upc} | {o.sku_descripcion} | Inv: {o.inventario_final_unidades}")

    # Nuevos clientes/SKUs sin registrar
    nuevos = session.query(RptForecastSinCatalogo).filter(
        RptForecastSinCatalogo.registrado == False
    ).all()
    if nuevos:
        logger.info(f"\n{'=' * 60}")
        logger.info(f"CLIENTES/SKUS NUEVOS SIN REGISTRAR: {len(nuevos)}")
        for n in nuevos:
            logger.info(f"  [{n.tipo_problema}] {n.cliente_nombre} | UPC {n.upc} | {n.descripcion}")

    logger.info(f"\n{'=' * 60}")
    logger.info(f"Total filas en reporte: {len(df)}")
    logger.info(f"Niveles: {sorted(df['level'].unique())}")
    logger.info(f"Clientes (nivel SKU): {df[df['level']==1]['cliente'].nunique() if 1 in df['level'].values else 0}")
    logger.info("PIPELINE COMPLETO.")


if __name__ == "__main__":
    main()
