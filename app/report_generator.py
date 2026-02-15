"""
report_generator.py — Generador de reporte agregado de 4 niveles.

Niveles:
    1. CLIENTE_SKU      — Detalle por cliente y SKU
    2. CLIENTE_FAMILIA   — Agregado por cliente y familia de SKU
    3. CLIENTE_TOTAL     — Agregado total por cliente
    4. TOTAL_TENKA       — Gran total

DOS en niveles agregados se RECALCULA desde valores agregados:
    DOS = (SUM_inventario_final / (SUM_sales_out_Nm / N)) * 30
    donde N = min(12, meses desde inicio de operaciones hasta mes anterior)
NO se promedia el DOS de los SKUs individuales.
"""

import logging

import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session

from .eligibility import _generate_date_id_range
from .models import (
    DimCliente,
    DimSku,
    FactDiasInventarioHistorico,
    FactInventoryUnconstrained,
    FactSalesIn,
    FactSalesInUnconstrained,
    FactSalesOut,
    FactSalesOutUnconstrained,
    FactStockCliente,
)
from .monthly_close import _compute_month_minus_n

logger = logging.getLogger(__name__)


def generate_aggregated_report(
    session: Session,
    mes_cierre_date_id: int,
    has_projections: bool = True,
) -> pd.DataFrame:
    """
    Genera un DataFrame con el reporte agregado de 4 niveles.

    24 periodos (12 historicos + 12 proyectados) por cada nivel.

    Args:
        session: Sesion SQLAlchemy activa.
        mes_cierre_date_id: Ultimo mes historico (YYYYMM).
        has_projections: Si hay datos proyectados disponibles.

    Returns:
        DataFrame con columnas:
        level, nivel_nombre, cliente, familia, sku_descripcion, upc,
        date_id, tipo, sales_out, sales_in, inventario_final, days_of_sale
    """
    # ── Construir ventanas de tiempo ──
    date_id_inicio = _compute_month_minus_n(mes_cierre_date_id, 11)
    historico_ids = _generate_date_id_range(date_id_inicio, mes_cierre_date_id)

    mes_m = mes_cierre_date_id % 100
    mes_a = mes_cierre_date_id // 100
    proy_start = (mes_a * 100 + mes_m + 1) if mes_m < 12 else ((mes_a + 1) * 100 + 1)
    projection_ids = []
    current = proy_start
    for _ in range(12):
        projection_ids.append(current)
        m = current % 100
        a = current // 100
        current = (a * 100 + m + 1) if m < 12 else ((a + 1) * 100 + 1)

    all_date_ids = historico_ids + projection_ids

    # ── Cargar mapeo SKU → familia ──
    sku_info = {}
    for row in session.query(DimSku.sku_id, DimSku.familia, DimSku.sku_descripcion, DimSku.upc).all():
        sku_info[row.sku_id] = {
            "familia": row.familia or "SIN_FAMILIA",
            "descripcion": row.sku_descripcion,
            "upc": row.upc,
        }

    # ── Cargar mapeo cliente_id → nombre ──
    client_names = {}
    for row in session.query(DimCliente.cliente_id, DimCliente.cliente_nombre).all():
        client_names[row.cliente_id] = row.cliente_nombre

    # ── NIVEL 1: CLIENTE_SKU ──
    level1_rows = _build_level1(
        session, historico_ids, projection_ids,
        sku_info, client_names, has_projections,
    )

    if not level1_rows:
        logger.warning("No hay datos para el reporte.")
        return pd.DataFrame()

    df1 = pd.DataFrame(level1_rows)

    # ── NIVEL 2: CLIENTE_FAMILIA ──
    df2 = _aggregate_level(df1, ["cliente", "familia"], "CLIENTE_FAMILIA", 2, all_date_ids)

    # ── NIVEL 3: CLIENTE_TOTAL ──
    df3 = _aggregate_level(df1, ["cliente"], "CLIENTE_TOTAL", 3, all_date_ids)

    # ── NIVEL 4: TOTAL_TENKA ──
    df4 = _aggregate_level(df1, [], "TOTAL_TENKA", 4, all_date_ids)

    # ── Combinar todos los niveles ──
    result = pd.concat([df1, df2, df3, df4], ignore_index=True)

    # Redondear
    for col in ["sales_out", "sales_in", "inventario_final", "days_of_sale"]:
        if col in result.columns:
            result[col] = pd.to_numeric(result[col], errors="coerce").round(2)

    # Ordenar
    result = result.sort_values(
        ["level", "cliente", "familia", "sku_descripcion", "date_id"],
        na_position="last",
    ).reset_index(drop=True)

    logger.info(
        "Reporte generado: %d filas, %d niveles",
        len(result),
        result["level"].nunique(),
    )
    return result


def _build_level1(
    session: Session,
    historico_ids: list[int],
    projection_ids: list[int],
    sku_info: dict,
    client_names: dict,
    has_projections: bool,
) -> list[dict]:
    """Construye filas del Nivel 1 (CLIENTE_SKU) desde las tablas de hechos."""
    rows = []

    # ── Datos historicos por cliente-SKU ──
    hist_so = (
        session.query(
            FactSalesOut.cliente_id,
            FactSalesOut.sku_id,
            FactSalesOut.date_id,
            func.sum(FactSalesOut.unidades_sales_out).label("val"),
        )
        .filter(FactSalesOut.date_id.in_(historico_ids))
        .group_by(FactSalesOut.cliente_id, FactSalesOut.sku_id, FactSalesOut.date_id)
        .all()
    )

    hist_si = (
        session.query(
            FactSalesIn.cliente_id,
            FactSalesIn.sku_id,
            FactSalesIn.date_id,
            func.sum(FactSalesIn.unidades_sales_in).label("val"),
        )
        .filter(FactSalesIn.date_id.in_(historico_ids))
        .group_by(FactSalesIn.cliente_id, FactSalesIn.sku_id, FactSalesIn.date_id)
        .all()
    )

    hist_inv = (
        session.query(
            FactStockCliente.cliente_id,
            FactStockCliente.sku_id,
            FactStockCliente.date_id,
            func.sum(FactStockCliente.inventario_final_unidades).label("val"),
        )
        .filter(FactStockCliente.date_id.in_(historico_ids))
        .group_by(FactStockCliente.cliente_id, FactStockCliente.sku_id, FactStockCliente.date_id)
        .all()
    )

    hist_dos = (
        session.query(
            FactDiasInventarioHistorico.cliente_id,
            FactDiasInventarioHistorico.sku_id,
            FactDiasInventarioHistorico.date_id,
            FactDiasInventarioHistorico.days_of_sale_historico.label("val"),
        )
        .filter(FactDiasInventarioHistorico.date_id.in_(historico_ids))
        .all()
    )

    # Construir lookups: (cliente_id, sku_id, date_id) -> value
    def _to_lookup(query_result):
        lk = {}
        for r in query_result:
            lk[(r.cliente_id, r.sku_id, r.date_id)] = float(r.val or 0)
        return lk

    so_lk = _to_lookup(hist_so)
    si_lk = _to_lookup(hist_si)
    inv_lk = _to_lookup(hist_inv)
    dos_lk = _to_lookup(hist_dos)

    # Determinar todos los pares historicos
    all_hist_keys = set(so_lk.keys()) | set(si_lk.keys()) | set(inv_lk.keys())
    hist_pairs = {(k[0], k[1]) for k in all_hist_keys}

    for cid, sid in sorted(hist_pairs):
        cliente_nombre = client_names.get(cid, f"CLIENTE_{cid}")
        info = sku_info.get(sid, {"familia": "SIN_FAMILIA", "descripcion": f"SKU_{sid}", "upc": 0})
        for did in historico_ids:
            rows.append({
                "level": 1,
                "nivel_nombre": "CLIENTE_SKU",
                "cliente": cliente_nombre,
                "familia": info["familia"],
                "sku_descripcion": info["descripcion"],
                "upc": info["upc"],
                "date_id": did,
                "tipo": "HISTORICO",
                "sales_out": so_lk.get((cid, sid, did), 0),
                "sales_in": si_lk.get((cid, sid, did), 0),
                "inventario_final": inv_lk.get((cid, sid, did), 0),
                "days_of_sale": dos_lk.get((cid, sid, did), None),
                "_cliente_id": cid,
                "_sku_id": sid,
            })

    # ── Datos proyectados por cliente-SKU ──
    if has_projections:
        proj_so = (
            session.query(
                FactSalesOutUnconstrained.cliente_id,
                FactSalesOutUnconstrained.sku_id,
                FactSalesOutUnconstrained.date_id,
                func.sum(FactSalesOutUnconstrained.unidades_sales_out_unc).label("val"),
            )
            .filter(FactSalesOutUnconstrained.date_id.in_(projection_ids))
            .group_by(
                FactSalesOutUnconstrained.cliente_id,
                FactSalesOutUnconstrained.sku_id,
                FactSalesOutUnconstrained.date_id,
            )
            .all()
        )

        proj_si = (
            session.query(
                FactSalesInUnconstrained.cliente_id,
                FactSalesInUnconstrained.sku_id,
                FactSalesInUnconstrained.date_id,
                func.sum(FactSalesInUnconstrained.unidades_sales_in_unc).label("val"),
            )
            .filter(FactSalesInUnconstrained.date_id.in_(projection_ids))
            .group_by(
                FactSalesInUnconstrained.cliente_id,
                FactSalesInUnconstrained.sku_id,
                FactSalesInUnconstrained.date_id,
            )
            .all()
        )

        proj_inv = (
            session.query(
                FactInventoryUnconstrained.cliente_id,
                FactInventoryUnconstrained.sku_id,
                FactInventoryUnconstrained.date_id,
                func.sum(FactInventoryUnconstrained.inventario_final_unc).label("inv"),
                FactInventoryUnconstrained.days_of_sale_unc.label("dos"),
            )
            .filter(FactInventoryUnconstrained.date_id.in_(projection_ids))
            .group_by(
                FactInventoryUnconstrained.cliente_id,
                FactInventoryUnconstrained.sku_id,
                FactInventoryUnconstrained.date_id,
                FactInventoryUnconstrained.days_of_sale_unc,
            )
            .all()
        )

        pso_lk = _to_lookup(proj_so)
        psi_lk = _to_lookup(proj_si)
        pinv_lk = {}
        pdos_lk = {}
        for r in proj_inv:
            key = (r.cliente_id, r.sku_id, r.date_id)
            pinv_lk[key] = float(r.inv or 0)
            pdos_lk[key] = float(r.dos) if r.dos is not None else None

        all_proj_keys = set(pso_lk.keys()) | set(psi_lk.keys()) | set(pinv_lk.keys())
        proj_pairs = {(k[0], k[1]) for k in all_proj_keys}

        for cid, sid in sorted(proj_pairs):
            cliente_nombre = client_names.get(cid, f"CLIENTE_{cid}")
            info = sku_info.get(sid, {"familia": "SIN_FAMILIA", "descripcion": f"SKU_{sid}", "upc": 0})
            for did in projection_ids:
                rows.append({
                    "level": 1,
                    "nivel_nombre": "CLIENTE_SKU",
                    "cliente": cliente_nombre,
                    "familia": info["familia"],
                    "sku_descripcion": info["descripcion"],
                    "upc": info["upc"],
                    "date_id": did,
                    "tipo": "PROYECTADO",
                    "sales_out": pso_lk.get((cid, sid, did), 0),
                    "sales_in": psi_lk.get((cid, sid, did), 0),
                    "inventario_final": pinv_lk.get((cid, sid, did), 0),
                    "days_of_sale": pdos_lk.get((cid, sid, did), None),
                    "_cliente_id": cid,
                    "_sku_id": sid,
                })

    return rows


def _aggregate_level(
    df_level1: pd.DataFrame,
    group_cols: list[str],
    nivel_nombre: str,
    level: int,
    all_date_ids: list[int],
) -> pd.DataFrame:
    """
    Agrega el nivel 1 a un nivel superior, recalculando DOS desde valores agregados.

    DOS = (SUM_inv / (SUM_SO_trailing_Nm / N)) * 30
    donde N = min(12, meses desde inicio de operaciones hasta mes anterior al calculo).

    Para niveles agregados (familia, cliente total, total tenka), el inicio de
    operaciones se determina como el primer mes con sales_out > 0 en el grupo.

    Args:
        df_level1: DataFrame del nivel 1 (CLIENTE_SKU).
        group_cols: Columnas de agrupacion (ej. ['cliente','familia'] para nivel 2).
        nivel_nombre: Nombre del nivel (ej. 'CLIENTE_FAMILIA').
        level: Numero de nivel (2, 3 o 4).
        all_date_ids: Lista completa de date_ids (historico + proyectado).

    Returns:
        DataFrame agregado con DOS recalculado.
    """
    if df_level1.empty:
        return pd.DataFrame()

    # Columnas de agrupacion para el aggregate + date_id + tipo
    agg_group = group_cols + ["date_id", "tipo"]

    # Sumar sales_out, sales_in, inventario_final
    agg = (
        df_level1.groupby(agg_group, dropna=False)
        .agg(
            sales_out=("sales_out", "sum"),
            sales_in=("sales_in", "sum"),
            inventario_final=("inventario_final", "sum"),
        )
        .reset_index()
    )

    # Recalcular DOS para cada grupo y date_id
    # DOS = (inv_final / (sum_so_trailing_Nm / N)) * 30
    # N = min(12, meses desde primera venta del grupo hasta mes anterior)

    # Construir lookup de sales_out por grupo y date_id
    so_by_group = {}
    for _, row in agg.iterrows():
        key = tuple(row[c] for c in group_cols) if group_cols else ("ALL",)
        if key not in so_by_group:
            so_by_group[key] = {}
        so_by_group[key][row["date_id"]] = row["sales_out"]

    # Determinar fecha de primera venta (sales_out > 0) por grupo
    first_sale_by_group: dict[tuple, int] = {}
    sorted_dates = sorted(all_date_ids)
    for key, so_dict in so_by_group.items():
        for did in sorted_dates:
            if so_dict.get(did, 0) > 0:
                first_sale_by_group[key] = did
                break

    # Calcular DOS
    dos_values = []
    for _, row in agg.iterrows():
        key = tuple(row[c] for c in group_cols) if group_cols else ("ALL",)
        current_did = row["date_id"]
        inv = row["inventario_final"]

        # Trailing N meses de sales_out ANTERIORES al mes actual
        # (no incluye el mes actual — DOS usa meses previos)
        idx = sorted_dates.index(current_did) if current_did in sorted_dates else -1
        if idx > 0:
            # Hasta 12 meses anteriores (sin incluir current_did)
            start = max(0, idx - 12)
            trailing_dids = sorted_dates[start:idx]
        else:
            trailing_dids = []

        # Ajustar por fecha de inicio de operaciones del grupo
        first_sale = first_sale_by_group.get(key)
        if first_sale is not None:
            trailing_dids = [d for d in trailing_dids if d >= first_sale]

        so_group = so_by_group.get(key, {})
        trailing_so = sum(so_group.get(d, 0) for d in trailing_dids)
        n_months = len(trailing_dids)

        if n_months > 0 and trailing_so > 0:
            avg_monthly_so = trailing_so / n_months
            dos = (inv / avg_monthly_so) * 30
        else:
            dos = None

        dos_values.append(dos)

    agg["days_of_sale"] = dos_values

    # Agregar columnas de identificacion del nivel
    agg["level"] = level
    agg["nivel_nombre"] = nivel_nombre

    # Llenar columnas faltantes
    if "cliente" not in agg.columns:
        agg["cliente"] = "TOTAL TENKA"
    if "familia" not in agg.columns:
        agg["familia"] = None
    agg["sku_descripcion"] = None
    agg["upc"] = None
    agg["_cliente_id"] = None
    agg["_sku_id"] = None

    # Reordenar columnas para consistencia
    cols = [
        "level", "nivel_nombre", "cliente", "familia", "sku_descripcion", "upc",
        "date_id", "tipo", "sales_out", "sales_in", "inventario_final",
        "days_of_sale", "_cliente_id", "_sku_id",
    ]
    for c in cols:
        if c not in agg.columns:
            agg[c] = None

    return agg[cols]
