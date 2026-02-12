"""
ingestion_core.py — Motor genérico de ingesta.

Funciones auxiliares compartidas por todos los jobs de ingesta:
    - Lectura de Excel con mapeo de columnas.
    - Resolución/creación de surrogate keys en dimensiones.
    - Derivación de date_id a partir de fechas.

Regla de negocio clave: celdas vacías → 0 en columnas numéricas.
"""

from datetime import date, datetime

import pandas as pd
from sqlalchemy.orm import Session

from .models import DimCliente, DimSku, DimTiempo


def read_excel_with_mapping(
    path: str,
    column_mapping: dict[str, str],
    sheet_name: int | str = 0,
) -> pd.DataFrame:
    """
    Lee un archivo Excel y renombra las columnas según column_mapping.

    Args:
        path: Ruta al archivo .xlsx.
        column_mapping: {nombre_en_excel: nombre_interno}.
            Solo las columnas presentes en el mapeo se conservan.
        sheet_name: Hoja a leer (0 = primera por defecto).

    Returns:
        DataFrame con columnas renombradas y NaN reemplazados por 0.
    """
    df = pd.read_excel(path, sheet_name=sheet_name)

    # Normalizar espacios en nombres de columna del Excel
    df.columns = df.columns.str.strip()

    # Renombrar solo las columnas que existan en el mapeo
    rename_map = {k.strip(): v for k, v in column_mapping.items() if k.strip() in df.columns}
    df = df.rename(columns=rename_map)

    # Conservar solo las columnas mapeadas
    cols_to_keep = [v for v in rename_map.values()]
    df = df[[c for c in cols_to_keep if c in df.columns]]

    # Regla de negocio: celdas vacías → 0 en columnas numéricas
    numeric_cols = df.select_dtypes(include=["number"]).columns
    df[numeric_cols] = df[numeric_cols].fillna(0)

    return df


# ─────────────────────────────────────────────
# Resolución de surrogate keys
# ─────────────────────────────────────────────


def get_or_create_sku_id(session: Session, upc: int, descripcion: str = "SIN DESCRIPCION") -> int:
    """
    Busca sku_id por UPC en dim_sku.
    Si no existe, lo crea con datos mínimos (upc + descripción placeholder).

    Estrategia: auto-create con datos mínimos para no bloquear la ingesta.
    El catálogo completo se carga con load_sku_catalog.

    Args:
        session: Sesión activa.
        upc: Código UPC del producto.
        descripcion: Descripción por defecto si se auto-crea.

    Returns:
        sku_id (surrogate key).
    """
    sku = session.query(DimSku).filter(DimSku.upc == int(upc)).first()
    if sku:
        return sku.sku_id

    nuevo = DimSku(upc=int(upc), sku_descripcion=descripcion)
    session.add(nuevo)
    session.flush()  # Obtener sku_id sin commit
    return nuevo.sku_id


def get_or_create_cliente_id(session: Session, cliente_nombre: str) -> int:
    """
    Busca cliente_id por nombre en dim_cliente.
    Si no existe, lo crea con status = 'Activo'.

    Args:
        session: Sesión activa.
        cliente_nombre: Nombre del cliente.

    Returns:
        cliente_id (surrogate key).
    """
    nombre = str(cliente_nombre).strip()
    cliente = session.query(DimCliente).filter(DimCliente.cliente_nombre == nombre).first()
    if cliente:
        return cliente.cliente_id

    nuevo = DimCliente(cliente_nombre=nombre, status="Activo")
    session.add(nuevo)
    session.flush()
    return nuevo.cliente_id


def get_date_id_from_date(session: Session, fecha: date) -> int:
    """
    Calcula date_id = YYYYMM a partir de una fecha y verifica que exista
    en dim_tiempo.

    Args:
        session: Sesión activa.
        fecha: Objeto date o datetime.

    Returns:
        date_id (entero YYYYMM).

    Raises:
        ValueError: Si el date_id no existe en dim_tiempo.
    """
    if isinstance(fecha, datetime):
        fecha = fecha.date()
    if isinstance(fecha, pd.Timestamp):
        fecha = fecha.date()

    date_id = fecha.year * 100 + fecha.month

    exists = session.get(DimTiempo, date_id)
    if not exists:
        raise ValueError(
            f"date_id {date_id} no existe en dim_tiempo. "
            f"Ejecuta ensure_time_dimension para el rango requerido."
        )
    return date_id


def compute_date_id(anio: int, mes: int) -> int:
    """
    Calcula date_id directamente sin verificar la base de datos.
    Útil cuando ya se garantizó que dim_tiempo está poblada.
    """
    return int(anio) * 100 + int(mes)


def safe_int(value, default: int = 0) -> int:
    """Convierte un valor a entero, retornando default si falla."""
    try:
        if pd.isna(value):
            return default
        return int(value)
    except (ValueError, TypeError):
        return default


def safe_date(value) -> date | None:
    """Convierte un valor a date, retornando None si falla."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return pd.to_datetime(value).date()
    except Exception:
        return None
