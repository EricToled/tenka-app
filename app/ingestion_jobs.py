"""
ingestion_jobs.py — Jobs de ingesta específicos para cada archivo fuente.

Cada función recibe la ruta al archivo Excel y usa ingestion_core para:
    1. Leer y mapear columnas.
    2. Resolver surrogate keys de dimensiones.
    3. Insertar registros en las tablas de hechos correspondientes.

Convención: commit al final de cada job, rollback en caso de error.
"""

import logging
from datetime import datetime

import pandas as pd
from sqlalchemy.orm import Session

from .database import SessionLocal
from .ingestion_core import (
    compute_date_id,
    get_or_create_cliente_id,
    get_or_create_sku_id,
    get_date_id_from_date,
    read_excel_with_mapping,
    safe_date,
    safe_int,
)
from .models import (
    DimSku,
    FactForecastSales,
    FactInventarioInterno,
    FactInventarioTransito,
    FactSalesIn,
    FactSalesOut,
    FactStockCliente,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# 6.1  Catálogo de SKU
# ─────────────────────────────────────────────

SKU_COLUMN_MAP = {
    "Descripcion": "sku_descripcion",
    "UPC": "upc",
    "Master pack": "master_pack",
    "Categoria": "categoria",
    "Tipo": "tipo",
    "Familia": "familia",
    "Status": "status",
}


def load_sku_catalog(path: str, session: Session | None = None) -> int:
    """
    Carga o actualiza el catálogo de SKU desde un Excel (SKU-Control-Table.xlsx).

    Returns:
        Número de registros procesados.
    """
    own_session = session is None
    if own_session:
        session = SessionLocal()

    try:
        df = read_excel_with_mapping(path, SKU_COLUMN_MAP)
        count = 0

        for _, row in df.iterrows():
            upc = safe_int(row.get("upc"))
            if upc == 0:
                continue  # UPC inválido, saltar

            existing = session.query(DimSku).filter(DimSku.upc == upc).first()

            if existing:
                # Actualizar campos
                existing.sku_descripcion = str(row.get("sku_descripcion", existing.sku_descripcion))
                existing.master_pack = safe_int(row.get("master_pack"), default=existing.master_pack)
                existing.categoria = str(row.get("categoria", "")) or existing.categoria
                existing.tipo = str(row.get("tipo", "")) or existing.tipo
                existing.familia = str(row.get("familia", "")) or existing.familia
                existing.status = str(row.get("status", "")) or existing.status
                existing.fecha_actualizacion = datetime.now()
            else:
                nuevo = DimSku(
                    upc=upc,
                    sku_descripcion=str(row.get("sku_descripcion", "SIN DESCRIPCION")),
                    master_pack=safe_int(row.get("master_pack")),
                    categoria=str(row.get("categoria", "")),
                    tipo=str(row.get("tipo", "")),
                    familia=str(row.get("familia", "")),
                    status=str(row.get("status", "")),
                )
                session.add(nuevo)

            count += 1

        session.commit()
        logger.info("load_sku_catalog: %d registros procesados desde %s", count, path)
        return count

    except Exception:
        session.rollback()
        logger.exception("Error en load_sku_catalog")
        raise
    finally:
        if own_session:
            session.close()


# ─────────────────────────────────────────────
# 6.2  Sales In
# ─────────────────────────────────────────────

SALES_IN_COLUMN_MAP = {
    "Fecha Inicial": "fecha_inicial",
    "Cliente": "cliente_nombre",
    "Unidad": "unidad_venta",
    "Descripcion": "descripcion",
    "UPC": "upc",
    "Suma de Venta Unidades": "unidades_sales_in",
}


def load_sales_in(path: str, session: Session | None = None) -> int:
    """
    Carga datos de Sales In desde un Excel (Sales-In-Table.xlsx).

    Returns:
        Número de registros insertados.
    """
    own_session = session is None
    if own_session:
        session = SessionLocal()

    try:
        df = read_excel_with_mapping(path, SALES_IN_COLUMN_MAP)
        count = 0

        for _, row in df.iterrows():
            fecha = safe_date(row.get("fecha_inicial"))
            if fecha is None:
                continue

            date_id = get_date_id_from_date(session, fecha)
            cliente_id = get_or_create_cliente_id(session, str(row.get("cliente_nombre", "")))
            upc = safe_int(row.get("upc"))
            if upc == 0:
                continue
            sku_id = get_or_create_sku_id(session, upc, str(row.get("descripcion", "SIN DESCRIPCION")))

            registro = FactSalesIn(
                date_id=date_id,
                cliente_id=cliente_id,
                sku_id=sku_id,
                unidades_sales_in=safe_int(row.get("unidades_sales_in")),
                unidad_venta=str(row.get("unidad_venta", "")) or None,
                fecha_inicial=fecha,
                fecha_final=None,
            )
            session.add(registro)
            count += 1

        session.commit()
        logger.info("load_sales_in: %d registros insertados desde %s", count, path)
        return count

    except Exception:
        session.rollback()
        logger.exception("Error en load_sales_in")
        raise
    finally:
        if own_session:
            session.close()


# ─────────────────────────────────────────────
# 6.3  Sales Out
# ─────────────────────────────────────────────

SALES_OUT_COLUMN_MAP = {
    "Año": "anio",
    "Mes Numero": "mes_numero",
    "Fecha Inicial": "fecha_inicial",
    "Fecha final": "fecha_final",
    "Cliente": "cliente_nombre",
    "UPC": "upc",
    "Descripcion": "descripcion",
    "Suma de Venta Unidades": "unidades_sales_out",
}


def load_sales_out(path: str, session: Session | None = None) -> int:
    """
    Carga datos de Sales Out desde un Excel (Sales-Out-Table.xlsx).

    Returns:
        Número de registros insertados.
    """
    own_session = session is None
    if own_session:
        session = SessionLocal()

    try:
        df = read_excel_with_mapping(path, SALES_OUT_COLUMN_MAP)
        count = 0

        for _, row in df.iterrows():
            fecha_inicial = safe_date(row.get("fecha_inicial"))
            fecha_final = safe_date(row.get("fecha_final"))
            if fecha_inicial is None or fecha_final is None:
                continue

            # Derivar date_id: preferir anio + mes_numero si existen, sino desde fecha
            anio = safe_int(row.get("anio"))
            mes = safe_int(row.get("mes_numero"))
            if anio > 0 and 1 <= mes <= 12:
                date_id = compute_date_id(anio, mes)
            else:
                date_id = get_date_id_from_date(session, fecha_inicial)

            cliente_id = get_or_create_cliente_id(session, str(row.get("cliente_nombre", "")))
            upc = safe_int(row.get("upc"))
            if upc == 0:
                continue
            sku_id = get_or_create_sku_id(session, upc, str(row.get("descripcion", "SIN DESCRIPCION")))

            registro = FactSalesOut(
                date_id=date_id,
                cliente_id=cliente_id,
                sku_id=sku_id,
                unidades_sales_out=safe_int(row.get("unidades_sales_out")),
                fecha_inicial=fecha_inicial,
                fecha_final=fecha_final,
            )
            session.add(registro)
            count += 1

        session.commit()
        logger.info("load_sales_out: %d registros insertados desde %s", count, path)
        return count

    except Exception:
        session.rollback()
        logger.exception("Error en load_sales_out")
        raise
    finally:
        if own_session:
            session.close()


# ─────────────────────────────────────────────
# 6.4  Stock Consolidado
# ─────────────────────────────────────────────

STOCK_CONSOLIDATED_COLUMN_MAP = {
    "Fecha Inicial": "fecha_inicial",
    "Fecha final": "fecha_final",
    "Intervalo": "intervalo",
    "Cliente": "cliente_nombre",
    "UPC": "upc",
    "Descripcion": "descripcion",
    "Sales Out": "unidades_sales_out",
    "Sales In": "unidades_sales_in",
    "Inventario Final": "inventario_final_unidades",
    "Mes": "mes_nombre",
    "Año": "anio",
}


def load_stock_consolidated(path: str, session: Session | None = None) -> int:
    """
    Carga datos de Stock Consolidado (Sales-Stock-Consolidated-Table.xlsx).

    Returns:
        Número de registros insertados.
    """
    own_session = session is None
    if own_session:
        session = SessionLocal()

    try:
        df = read_excel_with_mapping(path, STOCK_CONSOLIDATED_COLUMN_MAP)
        count = 0

        for _, row in df.iterrows():
            fecha_inicial = safe_date(row.get("fecha_inicial"))
            fecha_final = safe_date(row.get("fecha_final"))
            if fecha_inicial is None or fecha_final is None:
                continue

            # Derivar date_id desde anio + mes de fecha_inicial
            anio = safe_int(row.get("anio"))
            if anio > 0:
                date_id = compute_date_id(anio, fecha_inicial.month)
            else:
                date_id = get_date_id_from_date(session, fecha_inicial)

            cliente_id = get_or_create_cliente_id(session, str(row.get("cliente_nombre", "")))
            upc = safe_int(row.get("upc"))
            if upc == 0:
                continue
            sku_id = get_or_create_sku_id(session, upc, str(row.get("descripcion", "SIN DESCRIPCION")))

            registro = FactStockCliente(
                date_id=date_id,
                cliente_id=cliente_id,
                sku_id=sku_id,
                inventario_final_unidades=safe_int(row.get("inventario_final_unidades")),
                unidades_sales_in=safe_int(row.get("unidades_sales_in")),
                unidades_sales_out=safe_int(row.get("unidades_sales_out")),
                intervalo=str(row.get("intervalo", "")) or None,
                fecha_inicial=fecha_inicial,
                fecha_final=fecha_final,
            )
            session.add(registro)
            count += 1

        session.commit()
        logger.info("load_stock_consolidated: %d registros insertados desde %s", count, path)
        return count

    except Exception:
        session.rollback()
        logger.exception("Error en load_stock_consolidated")
        raise
    finally:
        if own_session:
            session.close()


# ─────────────────────────────────────────────
# 6.5  Inventario Interno
# ─────────────────────────────────────────────

INTERNAL_INVENTORY_COLUMN_MAP = {
    "Fecha": "fecha_registro",
    "UPC": "upc",
    "Disponible": "stock_interno_disponible",
}


def load_internal_inventory(path: str, session: Session | None = None) -> int:
    """
    Carga datos de Inventario Interno (Tabla-inventario-Interno.xlsx).

    Returns:
        Número de registros insertados.
    """
    own_session = session is None
    if own_session:
        session = SessionLocal()

    try:
        df = read_excel_with_mapping(path, INTERNAL_INVENTORY_COLUMN_MAP)
        count = 0

        for _, row in df.iterrows():
            fecha_registro = safe_date(row.get("fecha_registro"))
            if fecha_registro is None:
                continue

            date_id = get_date_id_from_date(session, fecha_registro)
            upc = safe_int(row.get("upc"))
            if upc == 0:
                continue
            sku_id = get_or_create_sku_id(session, upc)

            registro = FactInventarioInterno(
                date_id=date_id,
                sku_id=sku_id,
                stock_interno_disponible=safe_int(row.get("stock_interno_disponible")),
                fecha_registro=fecha_registro,
            )
            session.add(registro)
            count += 1

        session.commit()
        logger.info("load_internal_inventory: %d registros insertados desde %s", count, path)
        return count

    except Exception:
        session.rollback()
        logger.exception("Error en load_internal_inventory")
        raise
    finally:
        if own_session:
            session.close()


# ─────────────────────────────────────────────
# 6.6  Inventario en Tránsito
# ─────────────────────────────────────────────

TRANSIT_INVENTORY_COLUMN_MAP = {
    "Arrival Date": "arrival_date",
    "UPC": "upc",
    "Descripcion": "descripcion",
    "In Transit": "stock_en_transito",
    "In Transit Confirmed": "stock_en_transito_confirmado",
    "Status": "status",
}


def load_transit_inventory(path: str, session: Session | None = None) -> int:
    """
    Carga datos de Inventario en Tránsito (Tabla-inventartio-en-transito.xlsx).

    Returns:
        Número de registros insertados.
    """
    own_session = session is None
    if own_session:
        session = SessionLocal()

    try:
        df = read_excel_with_mapping(path, TRANSIT_INVENTORY_COLUMN_MAP)
        count = 0

        for _, row in df.iterrows():
            arrival = safe_date(row.get("arrival_date"))
            if arrival is None:
                continue

            date_id = get_date_id_from_date(session, arrival)
            upc = safe_int(row.get("upc"))
            if upc == 0:
                continue
            sku_id = get_or_create_sku_id(session, upc, str(row.get("descripcion", "SIN DESCRIPCION")))

            registro = FactInventarioTransito(
                date_id=date_id,
                sku_id=sku_id,
                stock_en_transito=safe_int(row.get("stock_en_transito")),
                stock_en_transito_confirmado=safe_int(row.get("stock_en_transito_confirmado")),
                arrival_date=arrival,
                status=str(row.get("status", "")) or None,
            )
            session.add(registro)
            count += 1

        session.commit()
        logger.info("load_transit_inventory: %d registros insertados desde %s", count, path)
        return count

    except Exception:
        session.rollback()
        logger.exception("Error en load_transit_inventory")
        raise
    finally:
        if own_session:
            session.close()


# ─────────────────────────────────────────────
# 6.7  Forecast 2026
# ─────────────────────────────────────────────

FORECAST_COLUMN_MAP = {
    "Fecha Inicial": "fecha_inicial",
    "Fecha Final": "fecha_final",
    "Intervalo": "intervalo",
    "Metric": "metric",
    "Budget": "budget",
    "UPC": "upc",
    "Descripcion": "descripcion",
    "Familia": "familia",
    "Valor": "unidades_forecast",
    "mes": "mes_nombre",
    "Año": "anio",
    "Cliente": "cliente_nombre",
}


def load_forecast_2026(path: str, session: Session | None = None) -> int:
    """
    Carga datos de Forecast Anual (Annual-sales-Forecast-2026.xlsx).

    Returns:
        Número de registros insertados.
    """
    own_session = session is None
    if own_session:
        session = SessionLocal()

    try:
        df = read_excel_with_mapping(path, FORECAST_COLUMN_MAP)

        # Filtrar filas de totales (no son un cliente real)
        EXCLUDED_CLIENTS = {"Total Tenka"}
        df = df[~df["cliente_nombre"].astype(str).str.strip().isin(EXCLUDED_CLIENTS)]

        count = 0

        for _, row in df.iterrows():
            fecha_inicial = safe_date(row.get("fecha_inicial"))
            fecha_final = safe_date(row.get("fecha_final"))
            if fecha_inicial is None or fecha_final is None:
                continue

            # Derivar date_id desde fecha_inicial
            date_id = get_date_id_from_date(session, fecha_inicial)

            cliente_nombre = str(row.get("cliente_nombre", "")).strip()
            if not cliente_nombre:
                continue
            cliente_id = get_or_create_cliente_id(session, cliente_nombre)

            upc = safe_int(row.get("upc"))
            if upc == 0:
                continue
            sku_id = get_or_create_sku_id(session, upc, str(row.get("descripcion", "SIN DESCRIPCION")))

            # unidades_forecast puede ser decimal
            try:
                unidades = float(row.get("unidades_forecast", 0))
            except (ValueError, TypeError):
                unidades = 0.0

            registro = FactForecastSales(
                date_id=date_id,
                cliente_id=cliente_id,
                sku_id=sku_id,
                metric=str(row.get("metric", "")) or None,
                budget=str(row.get("budget", "")) or None,
                unidades_forecast=unidades,
                intervalo=str(row.get("intervalo", "")) or None,
                fecha_inicial=fecha_inicial,
                fecha_final=fecha_final,
            )
            session.add(registro)
            count += 1

        session.commit()
        logger.info("load_forecast_2026: %d registros insertados desde %s", count, path)
        return count

    except Exception:
        session.rollback()
        logger.exception("Error en load_forecast_2026")
        raise
    finally:
        if own_session:
            session.close()
