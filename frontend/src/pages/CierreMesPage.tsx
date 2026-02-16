import { useState, useEffect } from 'react'
import { CheckCircle, Circle, Loader2, Play, AlertTriangle, Upload } from 'lucide-react'
import FileUploadCard from '../components/FileUploadCard'
import api from '../api/client'

/*
  5-step wizard:
    1. Detect closing month   -> GET /api/cierre/closing-month
    2. Upload Sales In/Out    -> POST /api/cierre/upload-sales  (paste TSV/CSV)
    3. Calculate inventory     -> POST /api/cierre/calculate-inventory  (R3+R4)
    4. Upload internal inv     -> POST /api/cierre/upload-internal-inv  (paste)
    5. Run projections         -> POST /api/cierre/run-projections  (R8)
*/

const fileUploads = [
  { label: 'Catalogo de SKU', endpoint: '/upload/sku', description: 'SKU Control Table.xlsx' },
  { label: 'Forecast', endpoint: '/upload/forecast', description: 'Annual sales Forecast.xlsx' },
]

export default function CierreMesPage() {
  const [step, setStep] = useState(1)

  // Step 1 state
  const [closingMonth, setClosingMonth] = useState<{ date_id: number; label: string; last_data_month: number } | null>(null)
  const [closingLoading, setClosingLoading] = useState(false)
  const [closingError, setClosingError] = useState<string | null>(null)

  // Step 2 state
  const [salesType, setSalesType] = useState<'sales_in' | 'sales_out'>('sales_in')
  const [pasteData, setPasteData] = useState('')
  const [uploadResult, setUploadResult] = useState<any>(null)
  const [uploadLoading, setUploadLoading] = useState(false)

  // Step 3 state
  const [invResult, setInvResult] = useState<any>(null)
  const [invLoading, setInvLoading] = useState(false)

  // Step 4 state
  const [intInvPaste, setIntInvPaste] = useState('')
  const [intInvResult, setIntInvResult] = useState<any>(null)
  const [intInvLoading, setIntInvLoading] = useState(false)

  // Step 5 state
  const [projResult, setProjResult] = useState<any>(null)
  const [projLoading, setProjLoading] = useState(false)

  // Auto-detect closing month on mount
  useEffect(() => {
    detectClosingMonth()
  }, [])

  const detectClosingMonth = async () => {
    setClosingLoading(true)
    setClosingError(null)
    try {
      const res = await api.get('/api/cierre/closing-month')
      setClosingMonth(res.data)
    } catch (err: any) {
      setClosingError(err?.response?.data?.detail || err.message)
    } finally {
      setClosingLoading(false)
    }
  }

  const handleUploadSales = async () => {
    if (!closingMonth || !pasteData.trim()) return
    setUploadLoading(true)
    setUploadResult(null)
    try {
      const res = await api.post('/api/cierre/upload-sales', {
        tsv_data: pasteData,
        tipo: salesType,
        mes_cierre_date_id: closingMonth.date_id,
      })
      setUploadResult(res.data)
    } catch (err: any) {
      setUploadResult({ error: err?.response?.data?.detail || err.message })
    } finally {
      setUploadLoading(false)
    }
  }

  const handleCalcInventory = async () => {
    if (!closingMonth) return
    setInvLoading(true)
    setInvResult(null)
    try {
      const res = await api.post('/api/cierre/calculate-inventory', {
        mes_cierre_date_id: closingMonth.date_id,
      })
      setInvResult(res.data)
    } catch (err: any) {
      setInvResult({ error: err?.response?.data?.detail || err.message })
    } finally {
      setInvLoading(false)
    }
  }

  const handleUploadIntInv = async () => {
    if (!closingMonth || !intInvPaste.trim()) return
    setIntInvLoading(true)
    setIntInvResult(null)
    try {
      const res = await api.post('/api/cierre/upload-internal-inv', {
        tsv_data: intInvPaste,
        mes_cierre_date_id: closingMonth.date_id,
      })
      setIntInvResult(res.data)
    } catch (err: any) {
      setIntInvResult({ error: err?.response?.data?.detail || err.message })
    } finally {
      setIntInvLoading(false)
    }
  }

  const handleRunProjections = async () => {
    if (!closingMonth) return
    setProjLoading(true)
    setProjResult(null)
    try {
      const res = await api.post('/api/cierre/run-projections', {
        mes_cierre_date_id: closingMonth.date_id,
      })
      setProjResult(res.data)
    } catch (err: any) {
      setProjResult({ error: err?.response?.data?.detail || err.message })
    } finally {
      setProjLoading(false)
    }
  }

  const StepIndicator = ({ num, label, active }: { num: number; label: string; active: boolean }) => (
    <button
      onClick={() => setStep(num)}
      className={`flex items-center gap-2 px-3 py-2 rounded-xl transition-all text-sm font-medium ${
        active
          ? 'bg-tenka-accent/15 text-tenka-accent border border-tenka-accent/30'
          : step > num
          ? 'text-tenka-success'
          : 'text-tenka-muted hover:text-tenka-text'
      }`}
    >
      {step > num ? <CheckCircle size={16} /> : <Circle size={16} />}
      <span className="hidden lg:inline">{label}</span>
      <span className="lg:hidden">P{num}</span>
    </button>
  )

  return (
    <div className="max-w-6xl">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-tenka-text">Cierre de Mes</h1>
        <p className="text-tenka-muted mt-2">
          Wizard de 5 pasos para el cierre mensual completo
        </p>
        {closingMonth && (
          <div className="mt-3 inline-flex items-center gap-2 bg-tenka-accent/10 text-tenka-accent px-4 py-2 rounded-xl text-sm font-medium">
            Mes a cerrar: {closingMonth.label} ({closingMonth.date_id})
          </div>
        )}
      </div>

      {/* Step indicators */}
      <div className="flex items-center gap-2 mb-8 flex-wrap">
        <StepIndicator num={1} label="Detectar mes" active={step === 1} />
        <div className="h-px w-4 bg-tenka-border hidden sm:block" />
        <StepIndicator num={2} label="Cargar ventas" active={step === 2} />
        <div className="h-px w-4 bg-tenka-border hidden sm:block" />
        <StepIndicator num={3} label="Inventario cliente" active={step === 3} />
        <div className="h-px w-4 bg-tenka-border hidden sm:block" />
        <StepIndicator num={4} label="Inventario interno" active={step === 4} />
        <div className="h-px w-4 bg-tenka-border hidden sm:block" />
        <StepIndicator num={5} label="Proyecciones" active={step === 5} />
      </div>

      {/* ─── Step 1: Detect closing month ─── */}
      {step === 1 && (
        <div className="max-w-2xl">
          <h2 className="text-lg font-semibold mb-4">Paso 1: Detectar mes de cierre</h2>

          {closingLoading && (
            <div className="flex items-center gap-2 text-tenka-muted">
              <Loader2 className="animate-spin" size={16} /> Detectando...
            </div>
          )}

          {closingError && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-xl p-4 mb-4">
              <div className="flex items-center gap-2 text-red-400 text-sm">
                <AlertTriangle size={16} /> {closingError}
              </div>
            </div>
          )}

          {closingMonth && (
            <div className="bg-tenka-card rounded-2xl border border-tenka-border p-6">
              <p className="text-sm text-tenka-muted mb-2">Ultimo mes con datos de stock:</p>
              <p className="text-2xl font-bold text-tenka-text">{closingMonth.last_data_month}</p>
              <p className="text-sm text-tenka-muted mt-4 mb-1">Siguiente mes a cerrar:</p>
              <p className="text-2xl font-bold text-tenka-accent">{closingMonth.label}</p>
            </div>
          )}

          {/* Optional: upload catalog / forecast */}
          <div className="mt-6">
            <h3 className="text-sm font-medium text-tenka-muted mb-3">Archivos opcionales (solo si hay cambios)</h3>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {fileUploads.map((u) => (
                <FileUploadCard key={u.endpoint} {...u} />
              ))}
            </div>
          </div>

          <div className="mt-6 text-right">
            <button
              onClick={() => setStep(2)}
              disabled={!closingMonth}
              className="bg-tenka-accent hover:bg-tenka-accent/80 disabled:opacity-50 text-white px-6 py-2.5 rounded-xl text-sm font-medium transition-all"
            >
              Siguiente: Cargar ventas
            </button>
          </div>
        </div>
      )}

      {/* ─── Step 2: Upload Sales In / Sales Out ─── */}
      {step === 2 && (
        <div className="max-w-3xl">
          <h2 className="text-lg font-semibold mb-4">Paso 2: Cargar Sales In / Sales Out</h2>

          <div className="bg-tenka-card rounded-2xl border border-tenka-border p-6">
            <div className="flex gap-4 mb-4">
              <button
                onClick={() => setSalesType('sales_in')}
                className={`px-4 py-2 rounded-xl text-sm font-medium transition-all ${
                  salesType === 'sales_in'
                    ? 'bg-tenka-accent/15 text-tenka-accent border border-tenka-accent/30'
                    : 'text-tenka-muted hover:text-tenka-text border border-tenka-border'
                }`}
              >
                Sales In
              </button>
              <button
                onClick={() => setSalesType('sales_out')}
                className={`px-4 py-2 rounded-xl text-sm font-medium transition-all ${
                  salesType === 'sales_out'
                    ? 'bg-tenka-accent2/15 text-tenka-accent2 border border-tenka-accent2/30'
                    : 'text-tenka-muted hover:text-tenka-text border border-tenka-border'
                }`}
              >
                Sales Out
              </button>
            </div>

            <p className="text-xs text-tenka-muted mb-2">
              Pega datos tabulados (TSV/CSV). Columnas: fecha_final, cliente, upc, unidades
            </p>
            <textarea
              value={pasteData}
              onChange={(e) => setPasteData(e.target.value)}
              rows={8}
              placeholder="fecha_final&#9;cliente&#9;upc&#9;unidades&#10;2025-07-31&#9;Liverpool&#9;12345&#9;100"
              className="w-full bg-tenka-surface border border-tenka-border rounded-xl px-4 py-3 text-xs text-tenka-text font-mono focus:outline-none focus:border-tenka-accent resize-y"
            />

            <button
              onClick={handleUploadSales}
              disabled={uploadLoading || !pasteData.trim()}
              className="mt-4 bg-tenka-accent hover:bg-tenka-accent/80 disabled:opacity-50 text-white px-6 py-2.5 rounded-xl text-sm font-medium transition-all flex items-center gap-2"
            >
              {uploadLoading ? <Loader2 className="animate-spin" size={16} /> : <Upload size={16} />}
              Cargar {salesType === 'sales_in' ? 'Sales In' : 'Sales Out'}
            </button>

            {uploadResult && (
              <pre className="mt-4 bg-tenka-surface rounded-xl p-4 text-xs text-tenka-muted overflow-auto max-h-48">
                {JSON.stringify(uploadResult, null, 2)}
              </pre>
            )}
          </div>

          <div className="mt-6 flex justify-between">
            <button onClick={() => setStep(1)} className="text-tenka-muted hover:text-tenka-text text-sm">
              Volver
            </button>
            <button
              onClick={() => setStep(3)}
              className="bg-tenka-accent hover:bg-tenka-accent/80 text-white px-6 py-2.5 rounded-xl text-sm font-medium transition-all"
            >
              Siguiente: Inventario cliente
            </button>
          </div>
        </div>
      )}

      {/* ─── Step 3: Calculate client inventory ─── */}
      {step === 3 && (
        <div className="max-w-xl">
          <h2 className="text-lg font-semibold mb-4">Paso 3: Calcular inventario en cliente</h2>

          <div className="bg-tenka-card rounded-2xl border border-tenka-border p-6">
            <p className="text-sm text-tenka-muted mb-4">
              Calcula inv_final = inv_anterior + SI - SO para cada par cliente-SKU.
              Si el resultado es negativo, aplica correccion retrospectiva y recalcula DOS.
            </p>
            <button
              onClick={handleCalcInventory}
              disabled={invLoading}
              className="w-full bg-tenka-accent2 hover:bg-tenka-accent2/80 disabled:opacity-50 text-white px-6 py-3 rounded-xl text-sm font-semibold transition-all flex items-center justify-center gap-2"
            >
              {invLoading ? <Loader2 className="animate-spin" size={16} /> : <Play size={16} />}
              Calcular inventario del mes {closingMonth?.date_id}
            </button>

            {invResult && (
              <div className="mt-4">
                {invResult.corrections > 0 && (
                  <div className="bg-yellow-500/10 border border-yellow-500/30 rounded-xl p-3 mb-3">
                    <p className="text-sm text-yellow-400">
                      <AlertTriangle size={14} className="inline mr-1" />
                      {invResult.corrections} pares con inventario negativo corregidos retrospectivamente
                    </p>
                  </div>
                )}
                <pre className="bg-tenka-surface rounded-xl p-4 text-xs text-tenka-muted overflow-auto max-h-48">
                  {JSON.stringify(invResult, null, 2)}
                </pre>
              </div>
            )}
          </div>

          <div className="mt-6 flex justify-between">
            <button onClick={() => setStep(2)} className="text-tenka-muted hover:text-tenka-text text-sm">
              Volver
            </button>
            <button
              onClick={() => setStep(4)}
              className="bg-tenka-accent hover:bg-tenka-accent/80 text-white px-6 py-2.5 rounded-xl text-sm font-medium transition-all"
            >
              Siguiente: Inventario interno
            </button>
          </div>
        </div>
      )}

      {/* ─── Step 4: Upload internal inventory ─── */}
      {step === 4 && (
        <div className="max-w-3xl">
          <h2 className="text-lg font-semibold mb-4">Paso 4: Cargar inventario interno</h2>

          <div className="bg-tenka-card rounded-2xl border border-tenka-border p-6">
            <p className="text-xs text-tenka-muted mb-2">
              Pega datos tabulados. Columnas: UPC, Inventario Cierre de Mes
            </p>
            <textarea
              value={intInvPaste}
              onChange={(e) => setIntInvPaste(e.target.value)}
              rows={8}
              placeholder="upc&#9;inventario_cierre_de_mes&#10;12345&#9;5000"
              className="w-full bg-tenka-surface border border-tenka-border rounded-xl px-4 py-3 text-xs text-tenka-text font-mono focus:outline-none focus:border-tenka-accent resize-y"
            />

            <button
              onClick={handleUploadIntInv}
              disabled={intInvLoading || !intInvPaste.trim()}
              className="mt-4 bg-tenka-accent hover:bg-tenka-accent/80 disabled:opacity-50 text-white px-6 py-2.5 rounded-xl text-sm font-medium transition-all flex items-center gap-2"
            >
              {intInvLoading ? <Loader2 className="animate-spin" size={16} /> : <Upload size={16} />}
              Cargar inventario interno
            </button>

            {intInvResult && (
              <pre className="mt-4 bg-tenka-surface rounded-xl p-4 text-xs text-tenka-muted overflow-auto max-h-48">
                {JSON.stringify(intInvResult, null, 2)}
              </pre>
            )}
          </div>

          <div className="mt-6 flex justify-between">
            <button onClick={() => setStep(3)} className="text-tenka-muted hover:text-tenka-text text-sm">
              Volver
            </button>
            <button
              onClick={() => setStep(5)}
              className="bg-tenka-accent hover:bg-tenka-accent/80 text-white px-6 py-2.5 rounded-xl text-sm font-medium transition-all"
            >
              Siguiente: Proyecciones
            </button>
          </div>
        </div>
      )}

      {/* ─── Step 5: Run projections ─── */}
      {step === 5 && (
        <div className="max-w-xl">
          <h2 className="text-lg font-semibold mb-4">Paso 5: Ejecutar proyecciones</h2>

          <div className="bg-tenka-card rounded-2xl border border-tenka-border p-6">
            <p className="text-sm text-tenka-muted mb-4">
              Ejecuta el proceso mensual completo (Unconstrained + Constrained Demand) para {closingMonth?.label}.
            </p>
            <button
              onClick={handleRunProjections}
              disabled={projLoading}
              className="w-full bg-tenka-accent2 hover:bg-tenka-accent2/80 disabled:opacity-50 text-white px-6 py-3 rounded-xl text-sm font-semibold transition-all flex items-center justify-center gap-2"
            >
              {projLoading ? <Loader2 className="animate-spin" size={16} /> : <Play size={16} />}
              Ejecutar proyecciones
            </button>

            {projResult && (
              <pre className="mt-4 bg-tenka-surface rounded-xl p-4 text-xs text-tenka-muted overflow-auto max-h-64">
                {JSON.stringify(projResult, null, 2)}
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
