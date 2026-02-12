# Tenka App — Guia de Demo

## Acceso

- **URL staging**: _(se actualizara tras deploy a Railway)_
- **Mes de cierre disponible**: `202507` (julio 2025)
- **Proyeccion**: agosto 2025 — julio 2026 (12 meses)

---

## Navegacion

| Vista | Ruta | Descripcion |
|-------|------|-------------|
| Reportes | `/reportes` | Vista principal de demos — KPIs y tablas comparativas |
| Cierre de Mes | `/cierre` | Wizard de 3 pasos para ingestar datos, cerrar mes y recalcular |
| Altas/Bajas | `/altas-bajas` | Gestion de clientes/SKUs nuevos y edicion de catalogos |
| Configuracion | `/configuracion` | Parametros operativos del sistema (solo lectura) |

---

## Datos de Prueba Interesantes

### Clientes para explorar

| Cliente | Caracteristica | Nota |
|---------|---------------|------|
| **Bodega Aurrera** | Cliente mas grande | Muchos SKUs, alta concentracion de ventas |
| **Walmart** | Cliente re-start | Proyeccion flat (use_trend=False), reanudo operaciones |
| **City Fresko** | Tendencia moderada | Buen ejemplo de proyeccion con tendencia lineal |
| **Waldo's** | Cliente nuevo | 5 SKUs en forecast, 0 datos historicos — CLIENTE_NUEVO |

### SKUs con ventas perdidas por OOS

| SKU | UPC | Unidades perdidas |
|-----|-----|------------------|
| Mikado Frutos Rojos 40 GH | 7506409020869 | 2,128 |
| Mikado Petalos De Rosa 40 GH | — | 1,514 |
| Difusor Frutos Rojos Sensair | — | 634 |
| Mikado Frutos Rojos 100 GH | — | 502 |

### Familias principales

- **Golden Hills** — familia con mayor volumen
- **Tenka 40ml** — productos flagship
- **Sachet Naturals** — productos de bajo costo, alto volumen
- **Sensair** — difusores, margen mas alto

---

## Flujo de Demo Recomendado

### 1. Reportes (vista principal)

1. Seleccionar mes cierre `202507` en FilterBar
2. Mostrar bloque **Demanda Unconstrained**:
   - KPIs: ventas totales SI 12M, DOS promedio, clientes riesgo sobreinventario
   - Tabla por familia: Golden Hills lidera volumen
   - Tabla por cliente: Bodega Aurrera domina
3. Mostrar bloque **Demanda Constrained**:
   - KPIs: ventas constrained vs perdidas por OOS
   - Tabla por familia: mostrar % perdida por restriccion
   - Tabla por cliente: identificar SKUs con OOS por cliente

### 2. Cierre de Mes (operativo)

1. **Paso 1**: Mostrar las 7 tarjetas de carga de archivos Excel
   - Explicar cada fuente de datos
2. **Paso 2**: Mostrar ejecucion de cierre mensual
   - El sistema inserta nuevo mes y elimina el mas antiguo (rolling 12M)
3. **Paso 3**: Recalculo de proyecciones
   - Unconstrained: modelo estadistico + DOS objectives
   - Constrained: POs, inventario interno, lost sales OOS

### 3. Altas/Bajas (gestion de catalogos)

1. Mostrar pares cliente-SKU nuevos detectados en forecast (Waldo's)
2. Demostrar "Marcar como dado de alta" → activa proyeccion desde forecast
3. Mostrar catalogo de clientes → editar termino_operaciones
4. Mostrar catalogo de SKUs → cambiar status o lead time

### 4. Configuracion

- Mostrar parametros del modelo: cobertura 4 meses, tendencia 8 meses min, DOS 60-90
- Explicar que son read-only en la interfaz

---

## Deployment Local (Docker Compose)

```bash
# 1. Levantar servicios
docker compose up --build -d

# 2. Restaurar datos
# Copiar dump al container de postgres
docker cp tenka_backup.dump tenka-app-postgres-1:/tmp/
docker exec tenka-app-postgres-1 pg_restore -U postgres -d tenka_db /tmp/tenka_backup.dump

# 3. Abrir http://localhost:3000
```

---

## Deployment Railway

### Servicios necesarios

1. **PostgreSQL**: Plugin nativo Railway → provee `DATABASE_URL`
2. **Backend**: Deploy con `Dockerfile.backend`
   - Variables: `DATABASE_URL` de Railway Postgres
3. **Frontend**: Deploy con `frontend/Dockerfile`
   - Build arg: `VITE_API_URL=""` (nginx proxy al backend)

### Restaurar datos en Railway

```bash
# Obtener DATABASE_URL de Railway
pg_restore -d "$RAILWAY_DATABASE_URL" tenka_backup.dump
```

---

## Notas Tecnicas

- El backend expone 25+ endpoints (17 originales + 8 frontend)
- Las proyecciones se recalculan con cada cierre mensual
- DOS objectives: >90 → baja a 90 en 6M → 60 en 6M mas; 60-90 → convergencia lineal a 60 en 12M
- POs se generan para todo el horizonte de 12 meses, con politica de L=4 meses de cobertura
- Modelo estadistico requiere minimo 8 meses con ventas para usar tendencia lineal
