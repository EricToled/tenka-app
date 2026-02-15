"""
run_full_with_reports.py — Pipeline completo: ingesta + Unconstrained + Constraint + Reportes.

Genera 4 reportes CSV:
1. Reporte 24M por cliente total: 12 hist + 12 proy con SI hist/constr, Inv, DOS
2. Reporte de inventario obsoleto por cliente
3. Reporte de ventas perdidas por OOS irrecuperables
4. Reporte de POs generadas a nivel SKU mes por mes
"""

import os
import sys
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("pipeline")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Data files are in the Tenka App root folder (3 levels up from .claude/worktrees/elegant-nash)
DATA_DIR = os.path.normpath(os.path.join(BASE_DIR, "..", "..", ".."))
# If the Excel files are in the same dir, use that instead
if os.path.exists(os.path.join(BASE_DIR, "SKU Control Table.xlsx")):
    DATA_DIR = BASE_DIR
logger.info(f"  DATA_DIR: {DATA_DIR}")

sys.path.insert(0, BASE_DIR)

os.environ.setdefault("DB_USER", "postgres")
os.environ.setdefault("DB_PASSWORD", "postgres")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "5432")
os.environ.setdefault("DB_NAME", "tenka_db")

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
from app.constraint_demand import run_constraint_process
from app.monthly_close import _compute_month_minus_n, _compute_month_plus_n
from app.eligibility import _generate_date_id_range
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
    logger.info("=" * 70)
    logger.info("PIPELINE COMPLETO — Ingesta + Unconstrained + Constraint + Reportes")
    logger.info("=" * 70)

    # ── 1. Verificar conexion ──
    logger.info("\nPASO 1: Verificando conexion a PostgreSQL...")
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            logger.info("  Conexion OK")
    except Exception as e:
        logger.error(f"  No se pudo conectar: {e}")
        return

    # Recrear tablas (fresh start)
    logger.info("\n  Recreando tablas (fresh start)...")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    logger.info("  Tablas creadas.")

    session = SessionLocal()

    try:
        # ── 2. Dimension tiempo ──
        logger.info("\nPASO 2: Inicializando dim_tiempo (2022-2028)...")
        n = ensure_time_dimension(session, 2022, 2028)
        logger.info(f"  {n} registros insertados en dim_tiempo.")
        session.commit()

        # ── 3. Ingesta ──
        logger.info("\nPASO 3: Ingesta de archivos Excel...")
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
                logger.warning(f"  SKIP: {filename} no encontrado en {DATA_DIR}")
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
        logger.info("\nPASO 4: Diagnostico post-ingesta...")
        n_sku = session.query(func.count(DimSku.sku_id)).scalar()
        n_cli = session.query(func.count(DimCliente.cliente_id)).scalar()
        n_t = session.query(func.count(DimTiempo.date_id)).scalar()
        logger.info(f"  dim_sku: {n_sku} | dim_cliente: {n_cli} | dim_tiempo: {n_t}")

        for tname, model, col in [
            ("fact_sales_in", FactSalesIn, FactSalesIn.date_id),
            ("fact_sales_out", FactSalesOut, FactSalesOut.date_id),
            ("fact_stock_cliente", FactStockCliente, FactStockCliente.date_id),
        ]:
            dates = session.query(distinct(col)).order_by(col).all()
            total = session.query(func.count(model.date_id)).scalar()
            logger.info(f"  {tname}: {total} rows, dates={[d[0] for d in dates]}")

        # Determinar mes de cierre
        max_date = session.query(func.max(FactStockCliente.date_id)).scalar()
        if max_date is None:
            logger.error("No hay datos en fact_stock_cliente. Abortando.")
            return
        mes_cierre = max_date
        logger.info(f"\n  >>> Mes de cierre: {mes_cierre}")

        # ── 5. DOS historicos ──
        logger.info("\nPASO 5: Construyendo DOS historicos...")
        dos_count = build_historical_dos(session, mes_cierre)
        logger.info(f"  {dos_count} registros DOS historico.")
        session.commit()

        # ── 6. Elegibilidad ──
        logger.info("\nPASO 6: Determinando elegibilidad...")
        eligibility = get_eligible_client_skus(session, mes_cierre)
        logger.info(f"  Elegibles: {len(eligibility.eligible)} | Graduating: {len(eligibility.graduating)} | Obsoletos: {eligibility.obsolete_count}")
        session.commit()

        # ── 6.5. Nuevos clientes/SKUs ──
        logger.info("\nPASO 6.5: Detectando clientes/SKUs nuevos en forecast...")
        new_det = detect_and_report_new_client_skus(session, mes_cierre)
        logger.info(f"  Nuevos: clientes={len(new_det['new_clients'])}, SKUs={len(new_det['new_skus'])}, baja={len(new_det['baja_skus'])}")
        session.commit()

        has_proj = bool(eligibility.eligible)
        if has_proj:
            # ── 7. Sales Out Unconstrained ──
            logger.info("\nPASO 7: Proyectando Sales Out Unconstrained...")
            so_count = project_sales_out_unconstrained(session, eligibility.eligible, mes_cierre)
            logger.info(f"  {so_count} registros Sales Out Unc.")
            session.commit()

            # ── 8. Sales In + Inventario Unconstrained ──
            logger.info("\nPASO 8: Proyectando Sales In + Inventario Unconstrained...")
            result = project_sales_in_and_inventory_unc(session, eligibility.eligible, mes_cierre)
            logger.info(f"  SI Unc: {result['sales_in_count']} | Inv Unc: {result['inventory_count']}")
            session.commit()
        else:
            logger.warning("  Sin elegibles para proyeccion estadistica.")

        # ── 8.5. Proyectar clientes nuevos ──
        logger.info("\nPASO 8.5: Proyectando clientes nuevos registrados...")
        new_proj = project_new_client_skus(session, mes_cierre)
        logger.info(f"  {new_proj} pares nuevos proyectados.")
        session.commit()

        # ── 9. FASE 3: Constraint Demand ──
        logger.info("\nPASO 9: Ejecutando Fase 3 — Constraint Demand...")
        if has_proj or new_proj > 0:
            constr_result = run_constraint_process(session, mes_cierre)
            session.commit()
            logger.info(f"  Estado: {constr_result['estado']}")
            logger.info(f"  {constr_result['mensaje']}")
        else:
            logger.warning("  Sin proyecciones, se omite Constraint Demand.")

        # ── 10. GENERAR REPORTES ──
        logger.info("\n" + "=" * 70)
        logger.info("PASO 10: Generando reportes...")
        logger.info("=" * 70)

        generate_report_24m_cliente(session, mes_cierre)
        generate_report_obsolete_inventory(session, mes_cierre)
        generate_report_lost_sales_oos(session)
        generate_report_pos_by_sku_month(session)

        logger.info("\n" + "=" * 70)
        logger.info("PIPELINE COMPLETO.")
        logger.info("=" * 70)

    except Exception as e:
        session.rollback()
        logger.exception(f"Error fatal: {e}")
        raise
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════
# REPORTE 1: 24 Meses por Cliente — 12 hist + 12 proy
# ═══════════════════════════════════════════════════════════════

def generate_report_24m_cliente(session, mes_cierre: int):
    """
    Reporte a nivel CLIENTE TOTAL AGREGADO con 24 meses:
    - 12 meses historicos: Sales In hist, Inventario Final hist, DOS hist
    - 12 meses proyectados: Sales In Constrained, Inv Final Constrained, DOS Constrained

    DOS se recalcula a nivel cliente agregado como:
        DOS = (SUM_inv_final / (SUM_SO_trailing_N / N)) * 30
    donde N = min(12, meses desde primera venta del cliente hasta mes anterior).
    NO se usa AVG de DOS individuales por SKU.
    """
    logger.info("\n  --- Reporte 1: 24M por Cliente Total ---")

    date_id_inicio = _compute_month_minus_n(mes_cierre, 11)
    historico_ids = _generate_date_id_range(date_id_inicio, mes_cierre)
    proy_start = _compute_month_plus_n(mes_cierre, 1)
    proy_end = _compute_month_plus_n(mes_cierre, 12)
    projection_ids = _generate_date_id_range(proy_start, proy_end)

    client_names = {r.cliente_id: r.cliente_nombre for r in session.query(DimCliente.cliente_id, DimCliente.cliente_nombre).all()}

    # Pre-cargar primera fecha de venta (Sales Out) por cliente
    # para determinar N = meses activos en lugar de siempre 12.
    first_sale_by_client: dict[int, int] = {}
    first_sale_rows = (
        session.query(
            FactSalesOut.cliente_id,
            func.min(FactSalesOut.date_id).label("first_date"),
        )
        .group_by(FactSalesOut.cliente_id)
        .all()
    )
    for r in first_sale_rows:
        first_sale_by_client[r.cliente_id] = r.first_date

    # Pre-cargar Sales Out historicas por (date_id, cliente_id) agregadas
    # Necesitamos hasta 24 meses atras para ventana trailing completa
    date_id_ventas_inicio = _compute_month_minus_n(date_id_inicio, 12)
    all_so_range = _generate_date_id_range(date_id_ventas_inicio, mes_cierre)
    hist_so_rows = (
        session.query(
            FactSalesOut.date_id,
            FactSalesOut.cliente_id,
            func.sum(FactSalesOut.unidades_sales_out).label("total"),
        )
        .filter(FactSalesOut.date_id.in_(all_so_range))
        .group_by(FactSalesOut.date_id, FactSalesOut.cliente_id)
        .all()
    )
    so_lookup: dict[tuple[int, int], float] = {}
    for r in hist_so_rows:
        so_lookup[(r.date_id, r.cliente_id)] = float(r.total or 0)

    # Pre-cargar Sales Out Unconstrained proyectadas por (date_id, cliente_id)
    proj_so_rows = (
        session.query(
            FactSalesOutUnconstrained.date_id,
            FactSalesOutUnconstrained.cliente_id,
            func.sum(FactSalesOutUnconstrained.unidades_sales_out_unc).label("total"),
        )
        .filter(FactSalesOutUnconstrained.date_id.in_(projection_ids))
        .group_by(FactSalesOutUnconstrained.date_id, FactSalesOutUnconstrained.cliente_id)
        .all()
    )
    for r in proj_so_rows:
        so_lookup[(r.date_id, r.cliente_id)] = float(r.total or 0)

    rows = []

    def _calc_dos_for_client(cid: int, current_did: int, inv_final: float, all_date_ids_up_to: list[int]) -> float | None:
        """Calcula DOS agregado para un cliente en un mes dado.

        DOS = (inv_final / (SUM_SO_trailing_N / N)) * 30
        N = min(12, meses desde primera venta hasta mes anterior al calculo)
        """
        # Hasta 12 meses ANTERIORES al mes actual (sin incluir current_did)
        idx = all_date_ids_up_to.index(current_did) if current_did in all_date_ids_up_to else -1
        if idx <= 0:
            return None  # Primer mes o no encontrado

        start = max(0, idx - 12)
        trailing_dids = all_date_ids_up_to[start:idx]

        # Ajustar por fecha de inicio de operaciones
        first_sale = first_sale_by_client.get(cid)
        if first_sale is not None:
            trailing_dids = [d for d in trailing_dids if d >= first_sale]

        if not trailing_dids:
            return None

        trailing_so = sum(so_lookup.get((d, cid), 0.0) for d in trailing_dids)
        n_months = len(trailing_dids)

        if n_months > 0 and trailing_so > 0:
            avg_monthly = trailing_so / n_months
            return (inv_final / avg_monthly) * 30
        return None

    # Lista completa de date_ids para busqueda de indice
    all_date_ids_ordered = sorted(all_so_range + historico_ids + projection_ids)
    # Eliminar duplicados manteniendo orden
    seen = set()
    all_date_ids_unique = []
    for d in all_date_ids_ordered:
        if d not in seen:
            seen.add(d)
            all_date_ids_unique.append(d)

    # ── Datos historicos por cliente ──
    for date_id in historico_ids:
        # Sales In historico por cliente
        si_rows = (
            session.query(
                FactSalesIn.cliente_id,
                func.sum(FactSalesIn.unidades_sales_in).label("sales_in"),
            )
            .filter(FactSalesIn.date_id == date_id)
            .group_by(FactSalesIn.cliente_id)
            .all()
        )

        # Inventario final historico por cliente
        inv_rows = (
            session.query(
                FactStockCliente.cliente_id,
                func.sum(FactStockCliente.inventario_final_unidades).label("inv"),
            )
            .filter(FactStockCliente.date_id == date_id)
            .group_by(FactStockCliente.cliente_id)
            .all()
        )

        si_map = {r.cliente_id: float(r.sales_in or 0) for r in si_rows}
        inv_map = {r.cliente_id: float(r.inv or 0) for r in inv_rows}

        all_clients = set(si_map.keys()) | set(inv_map.keys())
        for cid in all_clients:
            inv_val = inv_map.get(cid, 0)
            dos_val = _calc_dos_for_client(cid, date_id, inv_val, all_date_ids_unique)
            rows.append({
                "cliente": client_names.get(cid, f"CLIENTE_{cid}"),
                "date_id": date_id,
                "tipo": "HISTORICO",
                "sales_in": si_map.get(cid, 0),
                "inventario_final": inv_val,
                "days_of_sale": dos_val,
            })

    # ── Datos proyectados Constrained por cliente ──
    for date_id in projection_ids:
        # Sales In Constrained por cliente
        si_constr_rows = (
            session.query(
                FactSalesInConstrained.cliente_id,
                func.sum(FactSalesInConstrained.unidades_sales_in_constr).label("sales_in"),
            )
            .filter(FactSalesInConstrained.date_id == date_id)
            .group_by(FactSalesInConstrained.cliente_id)
            .all()
        )

        # Inventario Unconstrained por cliente
        inv_unc_rows = (
            session.query(
                FactInventoryUnconstrained.cliente_id,
                func.sum(FactInventoryUnconstrained.inventario_final_unc).label("inv"),
            )
            .filter(FactInventoryUnconstrained.date_id == date_id)
            .group_by(FactInventoryUnconstrained.cliente_id)
            .all()
        )

        si_map = {r.cliente_id: float(r.sales_in or 0) for r in si_constr_rows}
        inv_map = {r.cliente_id: float(r.inv or 0) for r in inv_unc_rows}

        all_clients = set(si_map.keys()) | set(inv_map.keys())
        for cid in all_clients:
            inv_val = inv_map.get(cid, 0)
            dos_val = _calc_dos_for_client(cid, date_id, inv_val, all_date_ids_unique)
            rows.append({
                "cliente": client_names.get(cid, f"CLIENTE_{cid}"),
                "date_id": date_id,
                "tipo": "PROYECTADO_CONSTRAINED",
                "sales_in": si_map.get(cid, 0),
                "inventario_final": inv_val,
                "days_of_sale": dos_val,
            })

    df = pd.DataFrame(rows)
    if df.empty:
        logger.warning("  Sin datos para reporte 24M.")
        return

    # Redondear
    for col in ["sales_in", "inventario_final", "days_of_sale"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").round(1)

    # Ordenar
    df = df.sort_values(["cliente", "date_id"]).reset_index(drop=True)

    # Guardar
    path = os.path.join(BASE_DIR, "reporte_24m_cliente_total.csv")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info(f"  Guardado: {path}")
    logger.info(f"  {len(df)} filas, {df['cliente'].nunique()} clientes")

    # Imprimir resumen
    for cliente in sorted(df["cliente"].unique()):
        cdf = df[df["cliente"] == cliente]
        hist = cdf[cdf["tipo"] == "HISTORICO"]
        proj = cdf[cdf["tipo"] == "PROYECTADO_CONSTRAINED"]
        logger.info(f"\n  CLIENTE: {cliente}")
        logger.info(f"  {'─' * 65}")
        logger.info(f"  {'Date':>8} | {'Tipo':<25} | {'Sales In':>12} | {'Inv Final':>12} | {'DOS':>6}")
        logger.info(f"  {'─' * 65}")
        for _, r in cdf.iterrows():
            dos_str = f"{r['days_of_sale']:.0f}" if pd.notna(r['days_of_sale']) else "N/A"
            logger.info(
                f"  {int(r['date_id']):>8} | {r['tipo']:<25} | {r['sales_in']:>12,.0f} | "
                f"{r['inventario_final']:>12,.0f} | {dos_str:>6}"
            )

    return df


# ═══════════════════════════════════════════════════════════════
# REPORTE 2: Inventario Obsoleto por Cliente
# ═══════════════════════════════════════════════════════════════

def generate_report_obsolete_inventory(session, mes_cierre: int):
    """Reporte de inventario obsoleto en cliente (SKUs dados de baja o sin demanda)."""
    logger.info("\n  --- Reporte 2: Inventario Obsoleto por Cliente ---")

    rows = (
        session.query(
            DimCliente.cliente_nombre,
            DimSku.upc,
            DimSku.sku_descripcion,
            DimSku.familia,
            RptInventarioObsoletoCliente.inventario_final_unidades,
            RptInventarioObsoletoCliente.date_id,
        )
        .join(DimCliente, RptInventarioObsoletoCliente.cliente_id == DimCliente.cliente_id)
        .join(DimSku, RptInventarioObsoletoCliente.sku_id == DimSku.sku_id)
        .order_by(DimCliente.cliente_nombre, RptInventarioObsoletoCliente.inventario_final_unidades.desc())
        .all()
    )

    if not rows:
        logger.info("  Sin registros de inventario obsoleto.")
        return pd.DataFrame()

    data = [{
        "cliente": r.cliente_nombre,
        "upc": r.upc,
        "sku_descripcion": r.sku_descripcion,
        "familia": r.familia,
        "inventario_obsoleto_unidades": float(r.inventario_final_unidades or 0),
        "date_id": r.date_id,
    } for r in rows]

    df = pd.DataFrame(data)
    path = os.path.join(BASE_DIR, "reporte_inventario_obsoleto.csv")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info(f"  Guardado: {path}")
    logger.info(f"  {len(df)} registros obsoletos")

    # Resumen por cliente
    summary = df.groupby("cliente").agg(
        skus_obsoletos=("upc", "count"),
        total_unidades=("inventario_obsoleto_unidades", "sum"),
    ).sort_values("total_unidades", ascending=False)

    logger.info(f"\n  {'Cliente':<30} | {'SKUs Obs':>10} | {'Unidades':>14}")
    logger.info(f"  {'─' * 60}")
    for cli, row in summary.iterrows():
        logger.info(f"  {cli:<30} | {row['skus_obsoletos']:>10} | {row['total_unidades']:>14,.0f}")

    return df


# ═══════════════════════════════════════════════════════════════
# REPORTE 3: Ventas Perdidas por OOS Irrecuperables
# ═══════════════════════════════════════════════════════════════

def generate_report_lost_sales_oos(session):
    """Reporte de ventas perdidas por Out-of-Stock irrecuperable en ventana de lead time."""
    logger.info("\n  --- Reporte 3: Ventas Perdidas por OOS Irrecuperables ---")

    rows = (
        session.query(
            DimSku.upc,
            DimSku.sku_descripcion,
            DimSku.familia,
            DimSku.lead_time_meses,
            RptLostSalesOos.lost_sales_total,
            RptLostSalesOos.date_id_inicio_lt,
            RptLostSalesOos.date_id_fin_lt,
            RptLostSalesOos.detalles,
        )
        .join(DimSku, RptLostSalesOos.sku_id == DimSku.sku_id)
        .order_by(RptLostSalesOos.lost_sales_total.desc())
        .all()
    )

    if not rows:
        logger.info("  Sin ventas perdidas por OOS.")
        return pd.DataFrame()

    import json
    data = []
    for r in rows:
        detalles = json.loads(r.detalles) if r.detalles else {}
        data.append({
            "upc": r.upc,
            "sku_descripcion": r.sku_descripcion,
            "familia": r.familia,
            "lead_time_meses": r.lead_time_meses or 4,
            "ventas_perdidas_total": float(r.lost_sales_total or 0),
            "ventana_lt_inicio": r.date_id_inicio_lt,
            "ventana_lt_fin": r.date_id_fin_lt,
            "detalles_por_mes": str(detalles),
        })

    df = pd.DataFrame(data)
    path = os.path.join(BASE_DIR, "reporte_ventas_perdidas_oos.csv")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info(f"  Guardado: {path}")

    total_lost = df["ventas_perdidas_total"].sum()
    logger.info(f"  {len(df)} SKUs con OOS | Total ventas perdidas: {total_lost:,.0f} unidades")

    logger.info(f"\n  {'UPC':<15} | {'SKU':<35} | {'Familia':<15} | {'LT':>3} | {'Lost Sales':>14}")
    logger.info(f"  {'─' * 90}")
    for _, r in df.iterrows():
        logger.info(
            f"  {r['upc']:<15} | {str(r['sku_descripcion'])[:35]:<35} | "
            f"{str(r['familia'])[:15]:<15} | {r['lead_time_meses']:>3} | "
            f"{r['ventas_perdidas_total']:>14,.0f}"
        )

    return df


# ═══════════════════════════════════════════════════════════════
# REPORTE 4: POs Generadas a Nivel SKU Mes por Mes
# ═══════════════════════════════════════════════════════════════

def generate_report_pos_by_sku_month(session):
    """Reporte de POs generadas a nivel total SKU mes por mes para el periodo proyectado."""
    logger.info("\n  --- Reporte 4: POs Generadas por SKU / Mes ---")

    rows = (
        session.query(
            DimSku.upc,
            DimSku.sku_descripcion,
            DimSku.familia,
            FactPoInterno.date_id_orden,
            FactPoInterno.date_id_llegada,
            func.sum(FactPoInterno.unidades_po).label("unidades_po"),
        )
        .join(DimSku, FactPoInterno.sku_id == DimSku.sku_id)
        .group_by(
            DimSku.upc,
            DimSku.sku_descripcion,
            DimSku.familia,
            FactPoInterno.date_id_orden,
            FactPoInterno.date_id_llegada,
        )
        .order_by(DimSku.upc, FactPoInterno.date_id_orden)
        .all()
    )

    if not rows:
        logger.info("  Sin POs generadas.")
        return pd.DataFrame()

    data = [{
        "upc": r.upc,
        "sku_descripcion": r.sku_descripcion,
        "familia": r.familia,
        "mes_orden": r.date_id_orden,
        "mes_llegada": r.date_id_llegada,
        "unidades_po": float(r.unidades_po or 0),
    } for r in rows]

    df = pd.DataFrame(data)
    path = os.path.join(BASE_DIR, "reporte_pos_sku_mes.csv")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info(f"  Guardado: {path}")

    total_pos = len(df)
    total_units = df["unidades_po"].sum()
    n_skus = df["upc"].nunique()
    logger.info(f"  {total_pos} POs para {n_skus} SKUs | Total unidades: {total_units:,.0f}")

    # Resumen por SKU
    sku_summary = df.groupby(["upc", "sku_descripcion"]).agg(
        n_pos=("unidades_po", "count"),
        total_unidades=("unidades_po", "sum"),
    ).sort_values("total_unidades", ascending=False)

    logger.info(f"\n  {'UPC':<15} | {'SKU':<35} | {'# POs':>6} | {'Total Unidades':>14}")
    logger.info(f"  {'─' * 75}")
    for (upc, desc), row in sku_summary.iterrows():
        logger.info(
            f"  {upc:<15} | {str(desc)[:35]:<35} | "
            f"{row['n_pos']:>6} | {row['total_unidades']:>14,.0f}"
        )

    # Detalle por mes
    logger.info(f"\n  Detalle por SKU / Mes:")
    for _, r in df.iterrows():
        logger.info(
            f"    UPC {r['upc']} | Orden: {r['mes_orden']} → Llegada: {r['mes_llegada']} | "
            f"{r['unidades_po']:>12,.0f} unidades"
        )

    return df


if __name__ == "__main__":
    main()
