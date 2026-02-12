"""
eligibility.py — Determinacion de cliente-SKU elegibles para proyeccion Unconstrained.

Reglas de elegibilidad (Seccion 3 de la especificacion):

1. Filtros de catalogo:
   - SKU: status <> 'Baja'
   - Cliente: termino_operaciones = 'Activo' o fecha_termino >= inicio ventana proyeccion

2. Ventas historicas minimas (12 meses):
   - Al menos 6 meses con Sales Out > 0
   - Al menos 1 de esos meses en los ultimos 6 meses historicos

3. Excepcion por ultimo mes:
   - Si tiene ventas Sales Out > 0 en el ultimo mes historico,
     se proyecta aunque no cumpla la regla de 6 meses.

4. Sin ventas en 12 meses pero con inventario:
   - No se proyecta.
   - Se reporta en rpt_inventario_obsoleto_cliente.

Adicionalmente calcula:
   - date_id_inicio_operaciones_cliente (primer mes con actividad)
   - meses_efectivos por cliente-SKU
"""

import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import and_, distinct, func
from sqlalchemy.orm import Session

from .models import (
    DimCliente,
    DimSku,
    DimTiempo,
    FactSalesOut,
    FactStockCliente,
    RptForecastSinCatalogo,
    RptInventarioObsoletoCliente,
)
from .monthly_close import _compute_month_minus_n

GRADUATION_THRESHOLD = 4  # meses con Sales Out real antes de proyeccion estadistica

logger = logging.getLogger(__name__)


MIN_MONTHS_FOR_TREND = 8  # minimo meses con ventas para usar regresion con tendencia


@dataclass
class EligibleClientSku:
    """Resultado de elegibilidad para un par cliente-SKU."""
    cliente_id: int
    sku_id: int
    meses_efectivos: int
    use_forecast_as_base: bool  # True si meses_efectivos < 3
    date_id_inicio_operaciones: int
    use_trend: bool = True  # True = regresion lineal con tendencia, False = promedio (sin slope)
    reason: str = ""  # razon de inclusion


@dataclass
class EligibilityResult:
    """Resultado completo del analisis de elegibilidad."""
    eligible: list[EligibleClientSku] = field(default_factory=list)
    graduating: list[EligibleClientSku] = field(default_factory=list)  # < 4 meses SO real, siguen con forecast
    obsolete_count: int = 0
    excluded_count: int = 0


def _parse_termino_operaciones(valor: str | None) -> str | date | None:
    """
    Interpreta el campo termino_operaciones de dim_cliente.

    Returns:
        'Activo' si el cliente sigue operando.
        date si tiene fecha de termino.
        None si el valor es nulo (se trata como Activo).
    """
    if valor is None or str(valor).strip().upper() == "ACTIVO":
        return "Activo"
    try:
        from datetime import datetime
        return datetime.strptime(str(valor).strip(), "%Y-%m-%d").date()
    except ValueError:
        return "Activo"


def _date_id_to_first_of_month(date_id: int) -> date:
    """Convierte un date_id YYYYMM al primer dia del mes."""
    anio = date_id // 100
    mes = date_id % 100
    return date(anio, mes, 1)


def _get_client_operation_start(
    session: Session,
    cliente_id: int,
) -> int | None:
    """
    Obtiene el primer date_id en que el cliente tiene actividad
    (ventas Sales Out > 0 o inventario en fact_stock_cliente).

    Returns:
        date_id mas antiguo con actividad, o None si no hay registros.
    """
    # Primer date_id con Sales Out > 0
    min_sales = (
        session.query(func.min(FactSalesOut.date_id))
        .filter(
            FactSalesOut.cliente_id == cliente_id,
            FactSalesOut.unidades_sales_out > 0,
        )
        .scalar()
    )

    # Primer date_id con stock
    min_stock = (
        session.query(func.min(FactStockCliente.date_id))
        .filter(FactStockCliente.cliente_id == cliente_id)
        .scalar()
    )

    candidates = [x for x in [min_sales, min_stock] if x is not None]
    return min(candidates) if candidates else None


def _get_sku_continuous_operation_start(
    session: Session,
    cliente_id: int,
    sku_id: int,
    historico_ids: list[int],
    gap_tolerance: int = 2,
) -> int:
    """
    Determina el inicio de la operacion CONTINUA mas reciente de un par
    cliente-SKU dentro de la ventana historica.

    Busca hacia atras desde el ultimo mes: si hay un gap (meses consecutivos
    sin ventas) mayor a gap_tolerance, el inicio real es el mes posterior
    al gap. Esto evita que meses de una operacion previa terminada distorsionen
    la regresion.

    Ejemplo Walmart: SO=[7,5,0,0,11,1,1967,3868,4291,4236,3990,4394]
    Gap de 2+ meses con SO ~ 0 antes de Feb 2025 -> inicio = 202502.

    Args:
        session: Sesion activa.
        cliente_id: ID del cliente.
        sku_id: ID del SKU.
        historico_ids: Lista de date_ids en la ventana historica (ordenados).
        gap_tolerance: Maximo de meses consecutivos sin ventas antes de
                       considerar que hay un corte de operacion. Default 2.

    Returns:
        date_id del inicio de operacion continua dentro de la ventana.
    """
    # Obtener meses con ventas > 0 para este par
    sales_months = set(
        r.date_id
        for r in session.query(FactSalesOut.date_id)
        .filter(
            FactSalesOut.cliente_id == cliente_id,
            FactSalesOut.sku_id == sku_id,
            FactSalesOut.date_id.in_(historico_ids),
            FactSalesOut.unidades_sales_out > 0,
        )
        .all()
    )

    if not sales_months:
        return historico_ids[0]

    # Recorrer la serie desde el final hacia atras
    # Buscar el primer gap > gap_tolerance contando desde el ultimo mes con ventas
    consecutive_zeros = 0
    start_idx = 0  # por default, toda la ventana

    for i in range(len(historico_ids) - 1, -1, -1):
        did = historico_ids[i]
        if did in sales_months:
            consecutive_zeros = 0
            start_idx = i
        else:
            consecutive_zeros += 1
            if consecutive_zeros > gap_tolerance:
                # Encontramos un gap grande: el inicio real es despues del gap
                start_idx = i + consecutive_zeros
                break

    # Asegurar que el indice esta en rango
    if start_idx >= len(historico_ids):
        start_idx = len(historico_ids) - 1

    return historico_ids[start_idx]


def _generate_date_id_range(start_date_id: int, end_date_id: int) -> list[int]:
    """Genera lista de date_ids (YYYYMM) desde start hasta end inclusive."""
    result = []
    current = start_date_id
    while current <= end_date_id:
        result.append(current)
        mes = current % 100
        anio = current // 100
        if mes == 12:
            current = (anio + 1) * 100 + 1
        else:
            current = anio * 100 + mes + 1
    return result


def get_eligible_client_skus(
    session: Session,
    mes_cierre_date_id: int,
) -> EligibilityResult:
    """
    Determina todas las combinaciones cliente-SKU elegibles para proyeccion.

    Args:
        session: Sesion de SQLAlchemy activa.
        mes_cierre_date_id: date_id del ultimo mes historico (YYYYMM).

    Returns:
        EligibilityResult con lista de elegibles, conteo de obsoletos y excluidos.

    Reglas aplicadas:
        - Filtros de catalogo (SKU activo, cliente no terminado)
        - 6 meses con ventas / 1 en ultimos 6 (o excepcion ultimo mes)
        - Inventario obsoleto reportado
        - Meses efectivos y flag use_forecast_as_base
    """
    result = EligibilityResult()

    # Ventana historica de 12 meses
    date_id_inicio = _compute_month_minus_n(mes_cierre_date_id, 11)
    date_id_mid = _compute_month_minus_n(mes_cierre_date_id, 5)  # inicio ultimos 6 meses
    historico_ids = _generate_date_id_range(date_id_inicio, mes_cierre_date_id)
    ultimos_6_ids = _generate_date_id_range(date_id_mid, mes_cierre_date_id)

    # Ventana de proyeccion: 12 meses despues del cierre
    date_id_proy_inicio_anio = mes_cierre_date_id // 100
    date_id_proy_inicio_mes = mes_cierre_date_id % 100
    if date_id_proy_inicio_mes == 12:
        proy_start = (date_id_proy_inicio_anio + 1) * 100 + 1
    else:
        proy_start = date_id_proy_inicio_anio * 100 + date_id_proy_inicio_mes + 1

    # 1. Obtener SKUs activos (no Baja)
    active_skus = {
        row.sku_id
        for row in session.query(DimSku.sku_id)
        .filter(DimSku.status != "Baja")
        .all()
    }

    # 2. Obtener clientes y su termino_operaciones
    clientes = session.query(DimCliente).all()
    active_clients = {}  # cliente_id -> DimCliente
    for c in clientes:
        termino = _parse_termino_operaciones(c.termino_operaciones)
        if termino == "Activo":
            active_clients[c.cliente_id] = c
        elif isinstance(termino, date):
            # Cliente activo si fecha_termino >= primer mes de proyeccion
            proy_first_date = _date_id_to_first_of_month(proy_start)
            if termino >= proy_first_date:
                active_clients[c.cliente_id] = c

    # 3. Obtener todas las combinaciones cliente-SKU con actividad historica
    #    desde fact_stock_cliente (fuente de verdad) y fact_sales_out
    client_sku_pairs = set()

    # Desde fact_stock_cliente
    stock_pairs = (
        session.query(
            FactStockCliente.cliente_id,
            FactStockCliente.sku_id,
        )
        .filter(FactStockCliente.date_id.in_(historico_ids))
        .distinct()
        .all()
    )
    client_sku_pairs.update((r.cliente_id, r.sku_id) for r in stock_pairs)

    # Desde fact_sales_out
    sales_pairs = (
        session.query(
            FactSalesOut.cliente_id,
            FactSalesOut.sku_id,
        )
        .filter(FactSalesOut.date_id.in_(historico_ids))
        .distinct()
        .all()
    )
    client_sku_pairs.update((r.cliente_id, r.sku_id) for r in sales_pairs)

    # 4. Evaluar cada par cliente-SKU
    for cliente_id, sku_id in client_sku_pairs:
        # Filtro catalogo
        if sku_id not in active_skus:
            result.excluded_count += 1
            continue
        if cliente_id not in active_clients:
            result.excluded_count += 1
            continue

        # Contar meses con Sales Out > 0 en la ventana historica
        meses_con_ventas = (
            session.query(func.count(distinct(FactSalesOut.date_id)))
            .filter(
                FactSalesOut.cliente_id == cliente_id,
                FactSalesOut.sku_id == sku_id,
                FactSalesOut.date_id.in_(historico_ids),
                FactSalesOut.unidades_sales_out > 0,
            )
            .scalar()
        ) or 0

        # Contar meses con ventas en ultimos 6 meses
        meses_ventas_ultimos_6 = (
            session.query(func.count(distinct(FactSalesOut.date_id)))
            .filter(
                FactSalesOut.cliente_id == cliente_id,
                FactSalesOut.sku_id == sku_id,
                FactSalesOut.date_id.in_(ultimos_6_ids),
                FactSalesOut.unidades_sales_out > 0,
            )
            .scalar()
        ) or 0

        # Verificar ventas en el ultimo mes historico
        ventas_ultimo_mes = (
            session.query(func.sum(FactSalesOut.unidades_sales_out))
            .filter(
                FactSalesOut.cliente_id == cliente_id,
                FactSalesOut.sku_id == sku_id,
                FactSalesOut.date_id == mes_cierre_date_id,
            )
            .scalar()
        ) or 0

        # Regla principal: >= 6 meses con ventas Y >= 1 en ultimos 6
        cumple_regla_principal = (meses_con_ventas >= 6 and meses_ventas_ultimos_6 >= 1)

        # Excepcion: ventas en ultimo mes
        cumple_excepcion = (ventas_ultimo_mes > 0)

        if cumple_regla_principal or cumple_excepcion:
            # Calcular inicio de operaciones CONTINUAS por par cliente-SKU
            # Esto detecta re-arranques (ej: Walmart cerro y reabrio)
            continuous_start = _get_sku_continuous_operation_start(
                session, cliente_id, sku_id, historico_ids
            )

            # Meses efectivos = desde inicio continuo hasta cierre
            effective_range = _generate_date_id_range(continuous_start, mes_cierre_date_id)
            meses_efectivos = len(effective_range)
            effective_start = continuous_start

            use_forecast = meses_efectivos < 3

            # Determinar si califica para regresion con tendencia:
            # Requiere >= MIN_MONTHS_FOR_TREND meses con ventas Y
            # que el inicio de operaciones sea anterior o igual al inicio
            # de la ventana historica (operacion madura, no reciente)
            use_trend = (
                meses_con_ventas >= MIN_MONTHS_FOR_TREND
                and continuous_start <= date_id_inicio
            )

            reason = "regla_principal" if cumple_regla_principal else "excepcion_ultimo_mes"
            if not use_trend and not use_forecast:
                reason += "|sin_tendencia"

            result.eligible.append(
                EligibleClientSku(
                    cliente_id=cliente_id,
                    sku_id=sku_id,
                    meses_efectivos=meses_efectivos,
                    use_forecast_as_base=use_forecast,
                    date_id_inicio_operaciones=effective_start,
                    use_trend=use_trend,
                    reason=reason,
                )
            )
        else:
            # Seccion 3.4: sin ventas pero con inventario → obsoleto
            if meses_con_ventas == 0:
                inv_final = (
                    session.query(func.sum(FactStockCliente.inventario_final_unidades))
                    .filter(
                        FactStockCliente.cliente_id == cliente_id,
                        FactStockCliente.sku_id == sku_id,
                        FactStockCliente.date_id == mes_cierre_date_id,
                    )
                    .scalar()
                )
                if inv_final is not None and inv_final > 0:
                    obsoleto = RptInventarioObsoletoCliente(
                        date_id=mes_cierre_date_id,
                        cliente_id=cliente_id,
                        sku_id=sku_id,
                        inventario_final_unidades=inv_final,
                    )
                    session.add(obsoleto)
                    result.obsolete_count += 1
                else:
                    result.excluded_count += 1
            else:
                result.excluded_count += 1

    # 5. Graduacion: pares registrados como nuevos con < 4 meses Sales Out real
    #    Estos pares se mueven de eligible a graduating si aun no tienen
    #    suficiente historial para proyeccion estadistica.
    registered_new_pairs = {
        (r.cliente_id, r.sku_id)
        for r in session.query(
            RptForecastSinCatalogo.cliente_id,
            RptForecastSinCatalogo.sku_id,
        )
        .filter(
            RptForecastSinCatalogo.registrado == True,
            RptForecastSinCatalogo.tipo_problema.in_(["CLIENTE_NUEVO", "SKU_NUEVO"]),
        )
        .all()
        if r.cliente_id is not None and r.sku_id is not None
    }

    if registered_new_pairs:
        still_eligible = []
        for item in result.eligible:
            pair = (item.cliente_id, item.sku_id)
            if pair in registered_new_pairs:
                # Contar meses con Sales Out real > 0
                meses_so_real = (
                    session.query(func.count(distinct(FactSalesOut.date_id)))
                    .filter(
                        FactSalesOut.cliente_id == item.cliente_id,
                        FactSalesOut.sku_id == item.sku_id,
                        FactSalesOut.unidades_sales_out > 0,
                    )
                    .scalar()
                ) or 0

                if meses_so_real < GRADUATION_THRESHOLD:
                    item.reason = f"graduating ({meses_so_real}/{GRADUATION_THRESHOLD} meses SO)"
                    result.graduating.append(item)
                else:
                    item.reason = "graduated"
                    still_eligible.append(item)
            else:
                still_eligible.append(item)
        result.eligible = still_eligible

    session.flush()
    logger.info(
        "Elegibilidad: %d elegibles, %d graduating, %d obsoletos, %d excluidos",
        len(result.eligible),
        len(result.graduating),
        result.obsolete_count,
        result.excluded_count,
    )
    return result
