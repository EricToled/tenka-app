"""
models.py — Modelos SQLAlchemy 2.x (Declarative) para esquema snowflake.

Convenciones:
    - Dimensiones: prefijo dim_
    - Hechos:      prefijo fact_
    - Surrogate keys: <tabla>_id (SERIAL / BIGSERIAL)
    - date_id:     entero YYYYMM
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────
# DIMENSIONES
# ─────────────────────────────────────────────


class DimSku(Base):
    __tablename__ = "dim_sku"

    sku_id = Column(Integer, primary_key=True, autoincrement=True)
    upc = Column(BigInteger, nullable=False, unique=True)
    sku_descripcion = Column(String(255), nullable=False)
    master_pack = Column(Integer)
    categoria = Column(String(100))
    tipo = Column(String(100))
    familia = Column(String(100))
    status = Column(String(50))
    lead_time_meses = Column(Integer, default=4)  # Lead time fábrica → CEDIS en meses
    fecha_creacion = Column(DateTime, server_default=func.now())
    fecha_actualizacion = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relaciones inversas
    fact_sales_in = relationship("FactSalesIn", back_populates="sku")
    fact_sales_out = relationship("FactSalesOut", back_populates="sku")
    fact_stock_cliente = relationship("FactStockCliente", back_populates="sku")
    fact_inventario_interno = relationship("FactInventarioInterno", back_populates="sku")
    fact_inventario_transito = relationship("FactInventarioTransito", back_populates="sku")
    fact_forecast_sales = relationship("FactForecastSales", back_populates="sku")


class DimCliente(Base):
    __tablename__ = "dim_cliente"

    cliente_id = Column(Integer, primary_key=True, autoincrement=True)
    cliente_nombre = Column(String(255), nullable=False, unique=True)
    canal = Column(String(100))
    region = Column(String(100))
    status = Column(String(50), default="Activo")
    termino_operaciones = Column(String(50), default="Activo")  # 'Activo' o fecha YYYY-MM-DD
    fecha_creacion = Column(DateTime, server_default=func.now())
    fecha_actualizacion = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relaciones inversas
    fact_sales_in = relationship("FactSalesIn", back_populates="cliente")
    fact_sales_out = relationship("FactSalesOut", back_populates="cliente")
    fact_stock_cliente = relationship("FactStockCliente", back_populates="cliente")
    fact_forecast_sales = relationship("FactForecastSales", back_populates="cliente")


class DimTiempo(Base):
    __tablename__ = "dim_tiempo"

    date_id = Column(Integer, primary_key=True)  # YYYYMM
    anio = Column(Integer, nullable=False)
    mes_numero = Column(Integer, nullable=False)
    mes_nombre = Column(String(20), nullable=False)
    fecha_inicial = Column(Date, nullable=False)
    fecha_final = Column(Date, nullable=False)


# ─────────────────────────────────────────────
# TABLAS DE HECHOS — HISTÓRICAS
# ─────────────────────────────────────────────


class FactSalesIn(Base):
    __tablename__ = "fact_sales_in"

    fact_sales_in_id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    unidades_sales_in = Column(Integer, nullable=False)
    unidad_venta = Column(String(50))
    fecha_inicial = Column(Date, nullable=False)
    fecha_final = Column(Date)
    fecha_creacion = Column(DateTime, server_default=func.now())

    # Relaciones
    tiempo = relationship("DimTiempo", lazy="joined")
    cliente = relationship("DimCliente", back_populates="fact_sales_in", lazy="joined")
    sku = relationship("DimSku", back_populates="fact_sales_in", lazy="joined")


class FactSalesOut(Base):
    __tablename__ = "fact_sales_out"

    fact_sales_out_id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    unidades_sales_out = Column(Integer, nullable=False)
    fecha_inicial = Column(Date, nullable=False)
    fecha_final = Column(Date, nullable=False)
    fecha_creacion = Column(DateTime, server_default=func.now())

    tiempo = relationship("DimTiempo", lazy="joined")
    cliente = relationship("DimCliente", back_populates="fact_sales_out", lazy="joined")
    sku = relationship("DimSku", back_populates="fact_sales_out", lazy="joined")


class FactStockCliente(Base):
    __tablename__ = "fact_stock_cliente"

    fact_stock_cliente_id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    inventario_final_unidades = Column(Integer, nullable=False)
    unidades_sales_in = Column(Integer)
    unidades_sales_out = Column(Integer)
    intervalo = Column(String(50))
    fecha_inicial = Column(Date, nullable=False)
    fecha_final = Column(Date, nullable=False)
    fecha_creacion = Column(DateTime, server_default=func.now())

    tiempo = relationship("DimTiempo", lazy="joined")
    cliente = relationship("DimCliente", back_populates="fact_stock_cliente", lazy="joined")
    sku = relationship("DimSku", back_populates="fact_stock_cliente", lazy="joined")


class FactInventarioInterno(Base):
    __tablename__ = "fact_inventario_interno"

    fact_inv_int_id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    stock_interno_disponible = Column(Integer, nullable=False)
    fecha_registro = Column(Date, nullable=False)
    fecha_creacion = Column(DateTime, server_default=func.now())

    tiempo = relationship("DimTiempo", lazy="joined")
    sku = relationship("DimSku", back_populates="fact_inventario_interno", lazy="joined")


class FactInventarioTransito(Base):
    __tablename__ = "fact_inventario_transito"

    fact_inv_trans_id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    stock_en_transito = Column(Integer, nullable=False)
    stock_en_transito_confirmado = Column(Integer)
    arrival_date = Column(Date, nullable=False)
    status = Column(String(50))
    fecha_creacion = Column(DateTime, server_default=func.now())

    tiempo = relationship("DimTiempo", lazy="joined")
    sku = relationship("DimSku", back_populates="fact_inventario_transito", lazy="joined")


# ─────────────────────────────────────────────
# TABLAS DE HECHOS — PROYECTADAS
# ─────────────────────────────────────────────


class FactForecastSales(Base):
    __tablename__ = "fact_forecast_sales"

    fact_forecast_id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    metric = Column(String(50))
    budget = Column(String(100))
    unidades_forecast = Column(Numeric(18, 4), nullable=False)
    intervalo = Column(String(50))
    fecha_inicial = Column(Date, nullable=False)
    fecha_final = Column(Date, nullable=False)
    fecha_creacion = Column(DateTime, server_default=func.now())

    tiempo = relationship("DimTiempo", lazy="joined")
    cliente = relationship("DimCliente", back_populates="fact_forecast_sales", lazy="joined")
    sku = relationship("DimSku", back_populates="fact_forecast_sales", lazy="joined")


# ─────────────────────────────────────────────
# CONTROL DE CIERRE MENSUAL
# ─────────────────────────────────────────────


class ControlCierreMensual(Base):
    __tablename__ = "control_cierre_mensual"

    control_id = Column(BigInteger, primary_key=True, autoincrement=True)
    mes_cierre_date_id = Column(Integer, nullable=False)
    estado = Column(String(50), nullable=False)
    mensaje = Column(Text)
    fecha_inicio = Column(DateTime)
    fecha_fin = Column(DateTime)


# ─────────────────────────────────────────────
# FASE 2: UNCONSTRAINED DEMAND
# ─────────────────────────────────────────────


class FactSalesOutUnconstrained(Base):
    __tablename__ = "fact_sales_out_unconstrained"

    fact_sales_out_unc_id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    unidades_sales_out_unc = Column(Numeric(18, 4), nullable=False)

    tiempo = relationship("DimTiempo", lazy="joined")
    cliente = relationship("DimCliente", lazy="joined")
    sku = relationship("DimSku", lazy="joined")


class FactSalesInUnconstrained(Base):
    __tablename__ = "fact_sales_in_unconstrained"

    fact_sales_in_unc_id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    unidades_sales_in_unc = Column(Numeric(18, 4), nullable=False)

    tiempo = relationship("DimTiempo", lazy="joined")
    cliente = relationship("DimCliente", lazy="joined")
    sku = relationship("DimSku", lazy="joined")


class FactInventoryUnconstrained(Base):
    __tablename__ = "fact_inventory_unconstrained"

    fact_inv_unc_id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    inventario_final_unc = Column(Numeric(18, 4), nullable=False)
    months_of_sale_unc = Column(Numeric(18, 4))
    days_of_sale_unc = Column(Numeric(18, 4))

    tiempo = relationship("DimTiempo", lazy="joined")
    cliente = relationship("DimCliente", lazy="joined")
    sku = relationship("DimSku", lazy="joined")


class FactDiasInventarioHistorico(Base):
    __tablename__ = "fact_dias_inventario_historico"

    fact_dos_hist_id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    inventario_final = Column(Numeric(18, 4), nullable=False)
    promedio_ventas_12m = Column(Numeric(18, 4))
    months_of_sale_historico = Column(Numeric(18, 4))
    days_of_sale_historico = Column(Numeric(18, 4))

    tiempo = relationship("DimTiempo", lazy="joined")
    cliente = relationship("DimCliente", lazy="joined")
    sku = relationship("DimSku", lazy="joined")


class RptInventarioObsoletoCliente(Base):
    __tablename__ = "rpt_inventario_obsoleto_cliente"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    inventario_final_unidades = Column(Numeric(18, 4), nullable=False)

    tiempo = relationship("DimTiempo", lazy="joined")
    cliente = relationship("DimCliente", lazy="joined")
    sku = relationship("DimSku", lazy="joined")


class RptForecastSinCatalogo(Base):
    __tablename__ = "rpt_forecast_sin_catalogo"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    cliente_nombre = Column(String(255))
    upc = Column(BigInteger)
    descripcion = Column(String(255))
    comentario = Column(Text)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"))
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"))
    tipo_problema = Column(String(50))
    registrado = Column(Boolean, default=False)
    fecha_registro = Column(DateTime)

    cliente = relationship("DimCliente", lazy="joined")
    sku = relationship("DimSku", lazy="joined")


class FactNewClientProjection(Base):
    __tablename__ = "fact_new_client_projection"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    unidades_sales_in = Column(Numeric(18, 4), nullable=False)
    inventario_final_acumulado = Column(Numeric(18, 4), nullable=False)
    origen = Column(String(50), default="FORECAST")

    tiempo = relationship("DimTiempo", lazy="joined")
    cliente = relationship("DimCliente", lazy="joined")
    sku = relationship("DimSku", lazy="joined")


# ─────────────────────────────────────────────
# FASE 3: CONSTRAINT DEMAND
# ─────────────────────────────────────────────


class FactPoInterno(Base):
    __tablename__ = "fact_po_interno"

    fact_po_id = Column(BigInteger, primary_key=True, autoincrement=True)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    date_id_orden = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    date_id_llegada = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    unidades_po = Column(Numeric(18, 4), nullable=False)
    fecha_creacion = Column(DateTime, server_default=func.now())

    sku = relationship("DimSku", lazy="joined")
    tiempo_orden = relationship("DimTiempo", foreign_keys=[date_id_orden], lazy="joined")
    tiempo_llegada = relationship("DimTiempo", foreign_keys=[date_id_llegada], lazy="joined")


class FactInventoryInternalConstrained(Base):
    __tablename__ = "fact_inventory_internal_constrained"

    fact_inv_int_constr_id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    inventario_final_int = Column(Numeric(18, 4), nullable=False)

    tiempo = relationship("DimTiempo", lazy="joined")
    sku = relationship("DimSku", lazy="joined")


class FactSalesInConstrained(Base):
    __tablename__ = "fact_sales_in_constrained"

    fact_sales_in_constr_id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    unidades_sales_in_constr = Column(Numeric(18, 4), nullable=False)

    tiempo = relationship("DimTiempo", lazy="joined")
    cliente = relationship("DimCliente", lazy="joined")
    sku = relationship("DimSku", lazy="joined")


class RptLostSalesOos(Base):
    __tablename__ = "rpt_lost_sales_oos"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    date_id_inicio_lt = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    date_id_fin_lt = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    lost_sales_total = Column(Numeric(18, 4), nullable=False)
    detalles = Column(Text)

    sku = relationship("DimSku", lazy="joined")


# ─────────────────────────────────────────────
# FASE B: REPORTE DE ASIGNACION + SO CONSTRAINED + INV CLIENTE CONSTRAINED
# ─────────────────────────────────────────────


class RptAsignacionInventario(Base):
    """Reporte editable de asignacion de inventario limitado (gate bloqueante)."""

    __tablename__ = "rpt_asignacion_inventario"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"), nullable=False)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    periodo_proyeccion = Column(Integer, nullable=False)  # 1, 2, 3 o 4
    si_unconstrained = Column(Numeric(18, 4), nullable=False)
    si_constrained_auto = Column(Numeric(18, 4), nullable=False)
    si_constrained_usuario = Column(Numeric(18, 4))  # NULL hasta aprobacion
    aprobado = Column(Boolean, default=False)
    fecha_creacion = Column(DateTime, server_default=func.now())
    fecha_aprobacion = Column(DateTime)

    sku = relationship("DimSku", lazy="joined")
    cliente = relationship("DimCliente", lazy="joined")
    tiempo = relationship("DimTiempo", lazy="joined")


class FactSalesOutConstrained(Base):
    """Sales Out Constrained por cliente-SKU-periodo."""

    __tablename__ = "fact_sales_out_constrained"

    fact_sales_out_constr_id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    unidades_sales_out_constr = Column(Numeric(18, 4), nullable=False)

    tiempo = relationship("DimTiempo", lazy="joined")
    cliente = relationship("DimCliente", lazy="joined")
    sku = relationship("DimSku", lazy="joined")


class FactInventoryClientConstrained(Base):
    """Inventario final del cliente Constrained por cliente-SKU-periodo."""

    __tablename__ = "fact_inventory_client_constrained"

    fact_inv_cli_constr_id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    inventario_final_constr = Column(Numeric(18, 4), nullable=False)

    tiempo = relationship("DimTiempo", lazy="joined")
    cliente = relationship("DimCliente", lazy="joined")
    sku = relationship("DimSku", lazy="joined")


# ─────────────────────────────────────────────
# FASE C: REPORTES VENTAS PERDIDAS + DOS CONSTRAINT
# ─────────────────────────────────────────────


class RptLostSalesSalesOut(Base):
    """Reporte de ventas perdidas Sales Out (SO_unc - SO_constr) periodos 1..L."""

    __tablename__ = "rpt_lost_sales_sales_out"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    periodo_proyeccion = Column(Integer, nullable=False)  # 1..4
    so_unconstrained = Column(Numeric(18, 4), nullable=False)
    so_constrained = Column(Numeric(18, 4), nullable=False)
    lost_sales = Column(Numeric(18, 4), nullable=False)  # so_unc - so_constr

    cliente = relationship("DimCliente", lazy="joined")
    sku = relationship("DimSku", lazy="joined")


class RptLostSalesSalesIn(Base):
    """Reporte de ventas perdidas Sales In (SI_unc - SI_constr) periodos 1..L."""

    __tablename__ = "rpt_lost_sales_sales_in"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    periodo_proyeccion = Column(Integer, nullable=False)  # 1..4
    si_unconstrained = Column(Numeric(18, 4), nullable=False)
    si_constrained = Column(Numeric(18, 4), nullable=False)
    lost_sales = Column(Numeric(18, 4), nullable=False)  # si_unc - si_constr

    cliente = relationship("DimCliente", lazy="joined")
    sku = relationship("DimSku", lazy="joined")


class FactDosConstraintCliente(Base):
    """DOS Constraint a nivel cliente-SKU-periodo."""

    __tablename__ = "fact_dos_constraint_cliente"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    cliente_id = Column(Integer, ForeignKey("dim_cliente.cliente_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    inventario_final_constr = Column(Numeric(18, 4), nullable=False)
    promedio_ventas_12m = Column(Numeric(18, 4))
    months_of_sale_constr = Column(Numeric(18, 4))
    days_of_sale_constr = Column(Numeric(18, 4))

    tiempo = relationship("DimTiempo", lazy="joined")
    cliente = relationship("DimCliente", lazy="joined")
    sku = relationship("DimSku", lazy="joined")


class FactDosConstraintInterno(Base):
    """DOS Constraint interno a nivel SKU-periodo."""

    __tablename__ = "fact_dos_constraint_interno"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    date_id = Column(Integer, ForeignKey("dim_tiempo.date_id"), nullable=False)
    sku_id = Column(Integer, ForeignKey("dim_sku.sku_id"), nullable=False)
    inventario_final_int_constr = Column(Numeric(18, 4), nullable=False)
    promedio_ventas_12m = Column(Numeric(18, 4))
    months_of_sale_constr = Column(Numeric(18, 4))
    days_of_sale_constr = Column(Numeric(18, 4))

    tiempo = relationship("DimTiempo", lazy="joined")
    sku = relationship("DimSku", lazy="joined")
