-- =============================================================================
-- DDL: Snowflake Schema - Sistema de Control de Inventarios y Ventas
-- Base de datos: PostgreSQL
-- =============================================================================

-- ─────────────────────────────────────────────
-- DIMENSIONES
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dim_sku (
    sku_id              SERIAL PRIMARY KEY,
    upc                 BIGINT NOT NULL UNIQUE,
    sku_descripcion     VARCHAR(255) NOT NULL,
    master_pack         INTEGER,
    categoria           VARCHAR(100),
    tipo                VARCHAR(100),
    familia             VARCHAR(100),
    status              VARCHAR(50),
    fecha_creacion      TIMESTAMP DEFAULT NOW(),
    fecha_actualizacion TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dim_cliente (
    cliente_id            SERIAL PRIMARY KEY,
    cliente_nombre        VARCHAR(255) NOT NULL UNIQUE,
    canal                 VARCHAR(100),
    region                VARCHAR(100),
    status                VARCHAR(50) DEFAULT 'Activo',
    termino_operaciones   VARCHAR(50) DEFAULT 'Activo',  -- 'Activo' o fecha YYYY-MM-DD
    fecha_creacion        TIMESTAMP DEFAULT NOW(),
    fecha_actualizacion   TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dim_tiempo (
    date_id         INTEGER PRIMARY KEY,        -- YYYYMM
    anio            INTEGER NOT NULL,
    mes_numero      INTEGER NOT NULL,           -- 1..12
    mes_nombre      VARCHAR(20) NOT NULL,       -- 'January', 'February', ...
    fecha_inicial   DATE NOT NULL,
    fecha_final     DATE NOT NULL
);

-- ─────────────────────────────────────────────
-- TABLAS DE HECHOS - HISTÓRICAS
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS fact_sales_in (
    fact_sales_in_id        BIGSERIAL PRIMARY KEY,
    date_id                 INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    cliente_id              INTEGER NOT NULL REFERENCES dim_cliente(cliente_id),
    sku_id                  INTEGER NOT NULL REFERENCES dim_sku(sku_id),
    unidades_sales_in       INTEGER NOT NULL,
    unidad_venta            VARCHAR(50),
    fecha_inicial           DATE NOT NULL,
    fecha_final             DATE,
    fecha_creacion          TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fact_sales_in_dim_keys
    ON fact_sales_in(date_id, cliente_id, sku_id);

CREATE TABLE IF NOT EXISTS fact_sales_out (
    fact_sales_out_id       BIGSERIAL PRIMARY KEY,
    date_id                 INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    cliente_id              INTEGER NOT NULL REFERENCES dim_cliente(cliente_id),
    sku_id                  INTEGER NOT NULL REFERENCES dim_sku(sku_id),
    unidades_sales_out      INTEGER NOT NULL,
    fecha_inicial           DATE NOT NULL,
    fecha_final             DATE NOT NULL,
    fecha_creacion          TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fact_sales_out_dim_keys
    ON fact_sales_out(date_id, cliente_id, sku_id);

CREATE TABLE IF NOT EXISTS fact_stock_cliente (
    fact_stock_cliente_id     BIGSERIAL PRIMARY KEY,
    date_id                   INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    cliente_id                INTEGER NOT NULL REFERENCES dim_cliente(cliente_id),
    sku_id                    INTEGER NOT NULL REFERENCES dim_sku(sku_id),
    inventario_final_unidades INTEGER NOT NULL,
    unidades_sales_in         INTEGER,
    unidades_sales_out        INTEGER,
    intervalo                 VARCHAR(50),
    fecha_inicial             DATE NOT NULL,
    fecha_final               DATE NOT NULL,
    fecha_creacion            TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fact_stock_cliente_dim_keys
    ON fact_stock_cliente(date_id, cliente_id, sku_id);

CREATE TABLE IF NOT EXISTS fact_inventario_interno (
    fact_inv_int_id           BIGSERIAL PRIMARY KEY,
    date_id                   INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    sku_id                    INTEGER NOT NULL REFERENCES dim_sku(sku_id),
    stock_interno_disponible  INTEGER NOT NULL,
    fecha_registro            DATE NOT NULL,
    fecha_creacion            TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fact_inv_int_dim_keys
    ON fact_inventario_interno(date_id, sku_id);

CREATE TABLE IF NOT EXISTS fact_inventario_transito (
    fact_inv_trans_id           BIGSERIAL PRIMARY KEY,
    date_id                     INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    sku_id                      INTEGER NOT NULL REFERENCES dim_sku(sku_id),
    stock_en_transito           INTEGER NOT NULL,
    stock_en_transito_confirmado INTEGER,
    arrival_date                DATE NOT NULL,
    status                      VARCHAR(50),
    fecha_creacion              TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fact_inv_trans_dim_keys
    ON fact_inventario_transito(date_id, sku_id);

-- ─────────────────────────────────────────────
-- TABLAS DE HECHOS - PROYECTADAS
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS fact_forecast_sales (
    fact_forecast_id        BIGSERIAL PRIMARY KEY,
    date_id                 INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    cliente_id              INTEGER NOT NULL REFERENCES dim_cliente(cliente_id),
    sku_id                  INTEGER NOT NULL REFERENCES dim_sku(sku_id),
    metric                  VARCHAR(50),
    budget                  VARCHAR(100),
    unidades_forecast       NUMERIC(18,4) NOT NULL,
    intervalo               VARCHAR(50),
    fecha_inicial           DATE NOT NULL,
    fecha_final             DATE NOT NULL,
    fecha_creacion          TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fact_forecast_dim_keys
    ON fact_forecast_sales(date_id, cliente_id, sku_id);

-- ─────────────────────────────────────────────
-- CONTROL DE CIERRE MENSUAL
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS control_cierre_mensual (
    control_id          BIGSERIAL PRIMARY KEY,
    mes_cierre_date_id  INTEGER NOT NULL,
    estado              VARCHAR(50) NOT NULL,   -- 'PENDIENTE','EN_PROCESO','COMPLETADO','ERROR'
    mensaje             TEXT,
    fecha_inicio        TIMESTAMP,
    fecha_fin           TIMESTAMP
);

-- =============================================================================
-- FASE 2: UNCONSTRAINED DEMAND
-- =============================================================================

-- 2.1 Sales Out proyectadas (Unconstrained)
CREATE TABLE IF NOT EXISTS fact_sales_out_unconstrained (
    fact_sales_out_unc_id  BIGSERIAL PRIMARY KEY,
    date_id                INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    cliente_id             INTEGER NOT NULL REFERENCES dim_cliente(cliente_id),
    sku_id                 INTEGER NOT NULL REFERENCES dim_sku(sku_id),
    unidades_sales_out_unc NUMERIC(18,4) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fact_sales_out_unc_keys
    ON fact_sales_out_unconstrained(date_id, cliente_id, sku_id);

-- 2.2 Sales In proyectadas (Unconstrained)
CREATE TABLE IF NOT EXISTS fact_sales_in_unconstrained (
    fact_sales_in_unc_id   BIGSERIAL PRIMARY KEY,
    date_id                INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    cliente_id             INTEGER NOT NULL REFERENCES dim_cliente(cliente_id),
    sku_id                 INTEGER NOT NULL REFERENCES dim_sku(sku_id),
    unidades_sales_in_unc  NUMERIC(18,4) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fact_sales_in_unc_keys
    ON fact_sales_in_unconstrained(date_id, cliente_id, sku_id);

-- 2.3 Inventario final y DOS proyectados (Unconstrained)
CREATE TABLE IF NOT EXISTS fact_inventory_unconstrained (
    fact_inv_unc_id        BIGSERIAL PRIMARY KEY,
    date_id                INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    cliente_id             INTEGER NOT NULL REFERENCES dim_cliente(cliente_id),
    sku_id                 INTEGER NOT NULL REFERENCES dim_sku(sku_id),
    inventario_final_unc   NUMERIC(18,4) NOT NULL,
    months_of_sale_unc     NUMERIC(18,4),
    days_of_sale_unc       NUMERIC(18,4)
);

CREATE INDEX IF NOT EXISTS idx_fact_inv_unc_keys
    ON fact_inventory_unconstrained(date_id, cliente_id, sku_id);

-- 2.4 Soporte histórico de DOS (Days of Sale)
CREATE TABLE IF NOT EXISTS fact_dias_inventario_historico (
    fact_dos_hist_id         BIGSERIAL PRIMARY KEY,
    date_id                  INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    cliente_id               INTEGER NOT NULL REFERENCES dim_cliente(cliente_id),
    sku_id                   INTEGER NOT NULL REFERENCES dim_sku(sku_id),
    inventario_final         NUMERIC(18,4) NOT NULL,
    promedio_ventas_12m      NUMERIC(18,4),
    months_of_sale_historico NUMERIC(18,4),
    days_of_sale_historico   NUMERIC(18,4)
);

CREATE INDEX IF NOT EXISTS idx_fact_dos_hist_keys
    ON fact_dias_inventario_historico(date_id, cliente_id, sku_id);

-- 2.5 Reporte de inventario obsoleto en cliente
CREATE TABLE IF NOT EXISTS rpt_inventario_obsoleto_cliente (
    id                        BIGSERIAL PRIMARY KEY,
    date_id                   INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    cliente_id                INTEGER NOT NULL REFERENCES dim_cliente(cliente_id),
    sku_id                    INTEGER NOT NULL REFERENCES dim_sku(sku_id),
    inventario_final_unidades NUMERIC(18,4) NOT NULL
);

-- 2.6 Clientes/SKUs en forecast que no están en catálogo
CREATE TABLE IF NOT EXISTS rpt_forecast_sin_catalogo (
    id                BIGSERIAL PRIMARY KEY,
    cliente_nombre    VARCHAR(255),
    upc               BIGINT,
    descripcion       VARCHAR(255),
    comentario        TEXT,
    cliente_id        INTEGER REFERENCES dim_cliente(cliente_id),
    sku_id            INTEGER REFERENCES dim_sku(sku_id),
    tipo_problema     VARCHAR(50),        -- 'CLIENTE_NUEVO','SKU_NUEVO','CLIENTE_Y_SKU_NUEVO','SKU_BAJA'
    registrado        BOOLEAN DEFAULT FALSE,
    fecha_registro    TIMESTAMP
);

-- 2.7 Proyección de clientes nuevos (basada en forecast)
CREATE TABLE IF NOT EXISTS fact_new_client_projection (
    id                          BIGSERIAL PRIMARY KEY,
    date_id                     INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    cliente_id                  INTEGER NOT NULL REFERENCES dim_cliente(cliente_id),
    sku_id                      INTEGER NOT NULL REFERENCES dim_sku(sku_id),
    unidades_sales_in           NUMERIC(18,4) NOT NULL,
    inventario_final_acumulado  NUMERIC(18,4) NOT NULL,
    origen                      VARCHAR(50) DEFAULT 'FORECAST'
);

CREATE INDEX IF NOT EXISTS idx_fact_new_client_proj_keys
    ON fact_new_client_projection(date_id, cliente_id, sku_id);

-- =============================================================================
-- FASE 3: CONSTRAINT DEMAND
-- =============================================================================

-- 3.0 Lead time dinámico en dim_sku
ALTER TABLE dim_sku ADD COLUMN IF NOT EXISTS lead_time_meses INTEGER DEFAULT 4;

-- 3.1 Órdenes de compra internas (POs)
CREATE TABLE IF NOT EXISTS fact_po_interno (
    fact_po_id      BIGSERIAL PRIMARY KEY,
    sku_id          INTEGER NOT NULL REFERENCES dim_sku(sku_id),
    date_id_orden   INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    date_id_llegada INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    unidades_po     NUMERIC(18,4) NOT NULL,
    fecha_creacion  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fact_po_interno_sku
    ON fact_po_interno(sku_id, date_id_orden);

CREATE INDEX IF NOT EXISTS idx_fact_po_interno_llegada
    ON fact_po_interno(sku_id, date_id_llegada);

-- 3.2 Inventario interno proyectado (constrained)
CREATE TABLE IF NOT EXISTS fact_inventory_internal_constrained (
    fact_inv_int_constr_id BIGSERIAL PRIMARY KEY,
    date_id                INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    sku_id                 INTEGER NOT NULL REFERENCES dim_sku(sku_id),
    inventario_final_int   NUMERIC(18,4) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fact_inv_int_constr_keys
    ON fact_inventory_internal_constrained(date_id, sku_id);

-- 3.3 Sales In Constrained (por cliente-SKU)
CREATE TABLE IF NOT EXISTS fact_sales_in_constrained (
    fact_sales_in_constr_id  BIGSERIAL PRIMARY KEY,
    date_id                  INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    cliente_id               INTEGER NOT NULL REFERENCES dim_cliente(cliente_id),
    sku_id                   INTEGER NOT NULL REFERENCES dim_sku(sku_id),
    unidades_sales_in_constr NUMERIC(18,4) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fact_sales_in_constr_keys
    ON fact_sales_in_constrained(date_id, cliente_id, sku_id);

-- 3.4 Reporte de ventas perdidas por OOS en ventana de lead time
CREATE TABLE IF NOT EXISTS rpt_lost_sales_oos (
    id                 BIGSERIAL PRIMARY KEY,
    sku_id             INTEGER NOT NULL REFERENCES dim_sku(sku_id),
    date_id_inicio_lt  INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    date_id_fin_lt     INTEGER NOT NULL REFERENCES dim_tiempo(date_id),
    lost_sales_total   NUMERIC(18,4) NOT NULL,
    detalles           TEXT
);
