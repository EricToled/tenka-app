import { useState } from 'react'
import { CheckCircle, Circle, Loader2, Play } from 'lucide-react'
import FileUploadCard from '../components/FileUploadCard'
import api from '../api/client'

const uploads = [
  { label: 'Catalogo de SKU', endpoint: '/upload/sku', description: 'SKU Control Table.xlsx' },
  { label: 'Sales In', endpoint: '/upload/sales-in', description: 'Sales In Table.xlsx' },
  { label: 'Sales Out', endpoint: '/upload/sales-out', description: 'Sales Out Table.xlsx' },
  { label: 'Stock Consolidado', endpoint: '/upload/stock-consolidated', description: 'Sales Stock Consolidated.xlsx' },
  { label: 'Inventario Interno', endpoint: '/upload/internal-inventory', description: 'Tabla inventario Interno.xlsx' },
  { label: 'Inventario en Transito', endpoint: '/upload/transit-inventory', description: 'Tabla inventario en transito.xlsx' },
  { label: 'Forecast', endpoint: '/upload/forecast', description: 'Annual sales Forecast.xlsx' },
]

export default function CierreMesPage() {
  const [step, setStep] = useState(1)
  const [mesCierre, setMesCierre] = useState('202507')
  const [closeResult, setCloseResult] = useState<any>(null)
  const [closeLoading, setCloseLoading] = useState(false)
  const [uncResult, setUncResult] = useState<any>(null)
  const [uncLoading, setUncLoading] = useState(false)
  const [constrResult, setConstrResult] = useState<any>(null)
  const [constrLoading, setConstrLoading] = useState(false)

  const handleClose = async () => {
    setCloseLoading(true)
    setCloseResult(null)
    try {
      const res = await api.post(`/monthly-close/${mesCierre}`)
      setCloseResult(res.data)
    } catch (err: any) {
      setCloseResult({ error: err?.response?.data?.detail || err.message })
    } finally {
      setCloseLoading(false)
    }
  }

  const handleUnconstrained = async () => {
    setUncLoading(true)
    setUncResult(null)
    try {
      const res = await api.post(`/unconstrained/run/${mesCierre}`)
      setUncResult(res.data)
    } catch (err: any) {
      setUncResult({ error: err?.response?.data?.detail || err.message })
    } finally {
      setUncLoading(false)
    }
  }

  const handleConstrained = async () => {
    setConstrLoading(true)
    setConstrResult(null)
    try {
      const res = await api.post(`/constraint/run/${mesCierre}`)
      setConstrResult(res.data)
    } catch (err: any) {
      setConstrResult({ error: err?.response?.data?.detail || err.message })
    } finally {
      setConstrLoading(false)
    }
  }

  const StepIndicator = ({ num, label, active }: { num: number; label: string; active: boolean }) => (
    <button
      onClick={() => setStep(num)}
      className={`flex items-center gap-2 px-4 py-2 rounded-xl transition-all text-sm font-medium ${
        active
          ? 'bg-tenka-accent/15 text-tenka-accent border border-tenka-accent/30'
          : step > num
          ? 'text-tenka-success'
          : 'text-tenka-muted hover:text-tenka-text'
      }`}
    >
      {step > num ? <CheckCircle size={16} /> : <Circle size={16} />}
      Paso {num}: {label}
    </button>
  )

  return (
    <div className="max-w-6xl">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-tenka-text">Cierre de Mes</h1>
        <p className="text-tenka-muted mt-2">
          Carga la informacion del periodo, ejecuta el cierre mensual y recalcula las proyecciones
        </p>
      </div>

      {/* Step indicators */}
      <div className="flex items-center gap-4 mb-8">
        <StepIndicator num={1} label="Cargar informacion" active={step === 1} />
        <div className="h-px w-8 bg-tenka-border" />
        <StepIndicator num={2} label="Ejecutar cierre" active={step === 2} />
        <div className="h-px w-8 bg-tenka-border" />
        <StepIndicator num={3} label="Recalcular proyecciones" active={step === 3} />
      </div>

      {/* Step 1: Upload */}
      {step === 1 && (
        <div>
          <h2 className="text-lg font-semibold mb-4">Cargar archivos del mes</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
            {uploads.map((u) => (
              <FileUploadCard key={u.endpoint} {...u} />
            ))}
          </div>
          <div className="mt-6 text-right">
            <button onClick={() => setStep(2)} className="bg-tenka-accent hover:bg-tenka-accent/80 text-white px-6 py-2.5 rounded-xl text-sm font-medium transition-all">
              Siguiente: Ejecutar cierre
            </button>
          </div>
        </div>
      )}

      {/* Step 2: Monthly Close */}
      {step === 2 && (
        <div className="max-w-xl">
          <h2 className="text-lg font-semibold mb-4">Ejecutar cierre mensual</h2>
          <div className="bg-tenka-card rounded-2xl border border-tenka-border p-6">
            <label className="block text-sm text-tenka-muted mb-2">Mes de cierre (YYYYMM)</label>
            <input
              type="text"
              value={mesCierre}
              onChange={(e) => setMesCierre(e.target.value)}
              className="bg-tenka-surface border border-tenka-border rounded-xl px-4 py-2.5 text-sm text-tenka-text w-full focus:outline-none focus:border-tenka-accent"
            />
            <button
              onClick={handleClose}
              disabled={closeLoading}
              className="mt-4 w-full bg-tenka-accent2 hover:bg-tenka-accent2/80 disabled:opacity-50 text-white px-6 py-3 rounded-xl text-sm font-semibold transition-all flex items-center justify-center gap-2"
            >
              {closeLoading ? <Loader2 className="animate-spin" size={16} /> : <Play size={16} />}
              Cerrar mes {mesCierre}
            </button>
            {closeResult && (
              <pre className="mt-4 bg-tenka-surface rounded-xl p-4 text-xs text-tenka-muted overflow-auto max-h-48">
                {JSON.stringify(closeResult, null, 2)}
              </pre>
            )}
          </div>
          <div className="mt-6 flex justify-between">
            <button onClick={() => setStep(1)} className="text-tenka-muted hover:text-tenka-text text-sm">
              Volver
            </button>
            <button onClick={() => setStep(3)} className="bg-tenka-accent hover:bg-tenka-accent/80 text-white px-6 py-2.5 rounded-xl text-sm font-medium transition-all">
              Siguiente: Recalcular
            </button>
          </div>
        </div>
      )}

      {/* Step 3: Recalculate */}
      {step === 3 && (
        <div className="max-w-2xl space-y-4">
          <h2 className="text-lg font-semibold mb-4">Recalcular proyecciones</h2>

          {/* Unconstrained */}
          <div className="bg-tenka-card rounded-2xl border border-tenka-border p-6">
            <h3 className="font-medium mb-2">Demanda Unconstrained</h3>
            <p className="text-sm text-tenka-muted mb-4">Recalcula Sales Out, Sales In e inventario proyectado para 12 meses</p>
            <button
              onClick={handleUnconstrained}
              disabled={uncLoading}
              className="bg-tenka-accent hover:bg-tenka-accent/80 disabled:opacity-50 text-white px-6 py-2.5 rounded-xl text-sm font-medium transition-all flex items-center gap-2"
            >
              {uncLoading ? <Loader2 className="animate-spin" size={16} /> : <Play size={16} />}
              Recalcular Unconstrained
            </button>
            {uncResult && (
              <pre className="mt-4 bg-tenka-surface rounded-xl p-4 text-xs text-tenka-muted overflow-auto max-h-48">
                {JSON.stringify(uncResult, null, 2)}
              </pre>
            )}
          </div>

          {/* Constrained */}
          <div className="bg-tenka-card rounded-2xl border border-tenka-border p-6">
            <h3 className="font-medium mb-2">Restricciones de Inventario (Constraint)</h3>
            <p className="text-sm text-tenka-muted mb-4">Genera POs, aplica restricciones de stock y calcula ventas perdidas por OOS</p>
            <button
              onClick={handleConstrained}
              disabled={constrLoading}
              className="bg-tenka-accent2 hover:bg-tenka-accent2/80 disabled:opacity-50 text-white px-6 py-2.5 rounded-xl text-sm font-medium transition-all flex items-center gap-2"
            >
              {constrLoading ? <Loader2 className="animate-spin" size={16} /> : <Play size={16} />}
              Aplicar restricciones (Constrained)
            </button>
            {constrResult && (
              <pre className="mt-4 bg-tenka-surface rounded-xl p-4 text-xs text-tenka-muted overflow-auto max-h-48">
                {JSON.stringify(constrResult, null, 2)}
              </pre>
            )}
          </div>

          <div className="mt-6">
            <button onClick={() => setStep(1)} className="text-tenka-muted hover:text-tenka-text text-sm">
              Volver al inicio
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
