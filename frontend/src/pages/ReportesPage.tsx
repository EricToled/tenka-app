/**
 * FIX BUG-01: Los filtros de cliente y familia ahora se envian como query params a la API.
 * FIX BUG-02: El selector de mes cierre se puebla dinamicamente desde /api/reports/available-months.
 */
import { useState, useEffect, useCallback } from 'react'
import { TrendingUp, Package, AlertTriangle, ShieldAlert, BarChart3, Users } from 'lucide-react'
import api from '../api/client'
import KpiCard from '../components/KpiCard'
import CollapsibleBlock from '../components/CollapsibleBlock'
import DataTable from '../components/DataTable'
import FilterBar from '../components/FilterBar'
import LoadingSpinner from '../components/LoadingSpinner'

interface UnconstrainedData {
  kpis: { ventas_unc_12m: number; inventario_objetivo_dias_promedio: number | null; clientes_riesgo_sobreinventario: number }
  by_familia: any[]
  by_cliente: any[]
}

interface ConstrainedData {
  kpis: { ventas_constr_12m: number; ventas_perdidas_oos: number; skus_riesgo_oos: number }
  by_familia: any[]
  by_cliente: any[]
}

interface MonthOption {
  date_id: number
  label: string
}

const numFmt = (v: any) => v !== null && v !== undefined ? Number(v).toLocaleString('es-MX', { maximumFractionDigits: 0 }) : '--'
const pctFmt = (v: any) => v !== null && v !== undefined ? `${Number(v).toFixed(1)}%` : '--'

export default function ReportesPage() {
  const [mesCierre, setMesCierre] = useState('202507')
  const [cliente, setCliente] = useState('')
  const [familia, setFamilia] = useState('')
  const [clientes, setClientes] = useState<string[]>([])
  const [familias, setFamilias] = useState<string[]>([])
  const [meses, setMeses] = useState<MonthOption[]>([])
  const [unc, setUnc] = useState<UnconstrainedData | null>(null)
  const [constr, setConstr] = useState<ConstrainedData | null>(null)
  const [loading, setLoading] = useState(false)

  // Load filter options (clients, familias, and available months)
  useEffect(() => {
    api.get('/api/catalog/clients').then(res => {
      setClientes(res.data.map((c: any) => c.cliente_nombre))
    }).catch(() => {})
    api.get('/api/catalog/familias').then(res => {
      setFamilias(res.data)
    }).catch(() => {})
    // FIX BUG-02: Cargar meses disponibles dinamicamente
    api.get('/api/reports/available-months').then(res => {
      setMeses(res.data)
      // Si hay meses disponibles y el valor actual no esta en la lista, usar el primero
      if (res.data.length > 0) {
        const currentValid = res.data.some((m: MonthOption) => String(m.date_id) === mesCierre)
        if (!currentValid) {
          setMesCierre(String(res.data[0].date_id))
        }
      }
    }).catch(() => {})
  }, [])

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      // FIX BUG-01: Enviar filtros de cliente y familia como query params
      const params: Record<string, string> = {}
      if (cliente) params.cliente = cliente
      if (familia) params.familia = familia

      const [uncRes, constrRes] = await Promise.all([
        api.get(`/api/reports/unconstrained-summary/${mesCierre}`, { params }),
        api.get(`/api/reports/constrained-summary/${mesCierre}`, { params }),
      ])
      setUnc(uncRes.data)
      setConstr(constrRes.data)
    } catch (err) {
      console.error('Error fetching reports', err)
    } finally {
      setLoading(false)
    }
  }, [mesCierre, cliente, familia])

  useEffect(() => { fetchData() }, [fetchData])

  // Unconstrained tables
  const uncFamCols = [
    { key: 'familia', label: 'Familia' },
    { key: 'ventas_unc_12m', label: 'Ventas Unc 12M', align: 'right' as const, format: numFmt },
    { key: 'inv_objetivo_meses', label: 'Inv Objetivo (meses)', align: 'right' as const },
    { key: 'inv_objetivo_dias', label: 'Inv Objetivo (dias)', align: 'right' as const, format: numFmt },
  ]

  const uncCliCols = [
    { key: 'cliente', label: 'Cliente' },
    { key: 'ventas_unc_12m', label: 'Ventas Unc 12M', align: 'right' as const, format: numFmt },
    { key: 'dos_promedio', label: 'DOS Promedio', align: 'right' as const },
    { key: 'skus_activos', label: 'SKUs Activos', align: 'right' as const },
  ]

  // Constrained tables
  const constrFamCols = [
    { key: 'familia', label: 'Familia' },
    { key: 'ventas_unc_12m', label: 'Ventas Unc 12M', align: 'right' as const, format: numFmt },
    { key: 'ventas_constr_12m', label: 'Ventas Constr 12M', align: 'right' as const, format: numFmt },
    { key: 'ventas_perdidas', label: 'Ventas Perdidas', align: 'right' as const, format: numFmt },
    { key: 'pct_perdida', label: '% Perdida', align: 'right' as const, format: pctFmt },
  ]

  const constrCliCols = [
    { key: 'cliente', label: 'Cliente' },
    { key: 'ventas_constr_12m', label: 'Ventas Constr 12M', align: 'right' as const, format: numFmt },
    { key: 'skus_con_oos', label: 'SKUs con OOS', align: 'right' as const },
    { key: 'ventas_perdidas', label: 'Ventas Perdidas', align: 'right' as const, format: numFmt },
  ]

  // Totals
  const uncFamTotal = unc ? {
    familia: 'Total Tenka',
    ventas_unc_12m: unc.by_familia.reduce((s, r) => s + r.ventas_unc_12m, 0),
    inv_objetivo_meses: '--',
    inv_objetivo_dias: '--',
  } : null

  const uncCliTotal = unc ? {
    cliente: 'Total Tenka',
    ventas_unc_12m: unc.by_cliente.reduce((s, r) => s + r.ventas_unc_12m, 0),
    dos_promedio: '--',
    skus_activos: unc.by_cliente.reduce((s, r) => s + r.skus_activos, 0),
  } : null

  const constrFamTotal = constr ? {
    familia: 'Total Tenka',
    ventas_unc_12m: constr.by_familia.reduce((s, r) => s + r.ventas_unc_12m, 0),
    ventas_constr_12m: constr.by_familia.reduce((s, r) => s + r.ventas_constr_12m, 0),
    ventas_perdidas: constr.by_familia.reduce((s, r) => s + r.ventas_perdidas, 0),
    pct_perdida: (() => {
      const tot_unc = constr.by_familia.reduce((s, r) => s + r.ventas_unc_12m, 0)
      const tot_lost = constr.by_familia.reduce((s, r) => s + r.ventas_perdidas, 0)
      return tot_unc > 0 ? (tot_lost / tot_unc * 100) : 0
    })(),
  } : null

  const constrCliTotal = constr ? {
    cliente: 'Total Tenka',
    ventas_constr_12m: constr.by_cliente.reduce((s, r) => s + r.ventas_constr_12m, 0),
    skus_con_oos: constr.kpis.skus_riesgo_oos,
    ventas_perdidas: constr.by_cliente.reduce((s, r) => s + r.ventas_perdidas, 0),
  } : null

  return (
    <div className="max-w-7xl">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-tenka-text">Reportes de Demanda e Inventario</h1>
        <p className="text-tenka-muted mt-2">
          Visualiza la demanda proyectada y las restricciones de inventario para tomar decisiones de compra y servicio al cliente
        </p>
      </div>

      {/* Filters */}
      <FilterBar
        mesCierre={mesCierre} setMesCierre={setMesCierre}
        cliente={cliente} setCliente={setCliente}
        familia={familia} setFamilia={setFamilia}
        clientes={clientes} familias={familias} meses={meses}
        onRefresh={fetchData} loading={loading}
      />

      {loading ? <LoadingSpinner text="Cargando reportes..." /> : (
        <div className="space-y-6">
          {/* Block 1: Unconstrained */}
          <CollapsibleBlock
            title="Demanda potencial (Unconstrained)"
            subtitle="Lo que el mercado demandaria si no hubiera restricciones de inventario"
          >
            {unc && (
              <>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
                  <KpiCard
                    label="Ventas Unconstrained 12M"
                    value={numFmt(unc.kpis.ventas_unc_12m)}
                    icon={TrendingUp}
                    color="accent"
                    subtitle="Unidades totales proyectadas"
                  />
                  <KpiCard
                    label="Inv. Objetivo Promedio"
                    value={unc.kpis.inventario_objetivo_dias_promedio ? `${unc.kpis.inventario_objetivo_dias_promedio} dias` : '--'}
                    icon={Package}
                    color="accent2"
                    subtitle="Days of Sale promedio"
                  />
                  <KpiCard
                    label="Clientes en Riesgo"
                    value={unc.kpis.clientes_riesgo_sobreinventario}
                    icon={AlertTriangle}
                    color="warning"
                    subtitle="Sobreinventario (MOS > 3)"
                  />
                </div>

                <DataTable
                  title="Vista por Familia"
                  columns={uncFamCols}
                  data={unc.by_familia}
                  totalsRow={uncFamTotal}
                />

                <DataTable
                  title="Vista por Cliente"
                  columns={uncCliCols}
                  data={unc.by_cliente}
                  totalsRow={uncCliTotal}
                />
              </>
            )}
          </CollapsibleBlock>

          {/* Block 2: Constrained */}
          <CollapsibleBlock
            title="Demanda limitada por inventario (Constrained)"
            subtitle="Lo que realmente podremos surtir considerando inventario interno, tiempos de entrega y ordenes de compra"
          >
            {constr && (
              <>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
                  <KpiCard
                    label="Ventas Constrained 12M"
                    value={numFmt(constr.kpis.ventas_constr_12m)}
                    icon={BarChart3}
                    color="success"
                    subtitle="Unidades reales esperadas"
                  />
                  <KpiCard
                    label="Ventas Perdidas OOS"
                    value={numFmt(constr.kpis.ventas_perdidas_oos)}
                    icon={ShieldAlert}
                    color="danger"
                    subtitle="Por falta de stock en lead time"
                  />
                  <KpiCard
                    label="SKUs en Riesgo OOS"
                    value={constr.kpis.skus_riesgo_oos}
                    icon={Users}
                    color="danger"
                    subtitle="Con ventas perdidas"
                  />
                </div>

                <DataTable
                  title="Vista por Familia"
                  columns={constrFamCols}
                  data={constr.by_familia}
                  totalsRow={constrFamTotal}
                />

                <DataTable
                  title="Vista por Cliente"
                  columns={constrCliCols}
                  data={constr.by_cliente}
                  totalsRow={constrCliTotal}
                />
              </>
            )}
          </CollapsibleBlock>
        </div>
      )}
    </div>
  )
}
