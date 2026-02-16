import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  CheckCircle, Circle, Loader2, Play, ArrowRight, ArrowLeft,
  AlertTriangle, Package, ClipboardPaste,
} from 'lucide-react'
import api from '../api/client'

interface ClosingMonth {
  date_id: number
  label: string
  last_data_month: number
}

interface StepResult {
  data?: any
  error?: string
}

export default function CierreMesPage() {
  const navigate = useNavigate()
  const [step, setStep] = useState(0)

  // Closing month
  const [closingMonth, setClosingMonth] = useState<ClosingMonth | null>(null)
  const [closingLoading, setClosingLoading] = useState(false)
  const [closingError, setClosingError] = useState<string | null>(null)

  // Step 1: Sales Out
  const [soPaste, setSoPaste] = useState('')
  const [soResult, setSoResult] = useState<StepResult | null>(null)
  const [soLoading, setSoLoading] = useState(false)

  // Step 2: Sales In
  const [siPaste, setSiPaste] = useState('')
  const [siResult, setSiResult] = useState<StepResult | null>(null)
  const [siLoading, setSiLoading] = useState(false)

  // Step 3: Calculate inventory
  const [invResult, setInvResult] = useState<StepResult | null>(null)
  const [invLoading, setInvLoading] = useState(false)

  // Step 4: Internal inventory
  const [intPaste, setIntPaste] = useState('')
  const [intResult, setIntResult] = useState<StepResult | null>(null)
  const [intLoading, setIntLoading] = useState(false)

  // Step 5: Projections
  const [projResult, setProjResult] = useState<StepResult | null>(null)
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
      setClosingError(err?.response?.data?.detail || 'Error detectando mes de cierre')
    } finally {
      setClosingLoading(false)
    }
  }

  const handleUploadSO = async () => {
    if (!closingMonth || !soPaste.trim()) return
    setSoLoading(true)
    setSoResult(null)
    try {
      const res = await api.post('/api/cierre/upload-sales', {
        tsv_data: soPaste,
        tipo: 'sales_out',
        mes_cierre_date_id: closingMonth.date_id,
      })
      setSoResult({ data: res.data })
    } catch (err: any) {
      setSoResult({ error: err?.response?.data?.detail || err.message })
    } finally {
      setSoLoading(false)
    }
  }

  const handleUploadSI = async () => {
    if (!closingMonth || !siPaste.trim()) return
    setSiLoading(true)
    setSiResult(null)
    try {
      const res = await api.post('/api/cierre/upload-sales', {
        tsv_data: siPaste,
        tipo: 'sales_in',
        mes_cierre_date_id: closingMonth.date_id,
      })
      setSiResult({ data: res.data })
    } catch (err: any) {
      setSiResult({ error: err?.response?.data?.detail || err.message })
    } finally {
      setSiLoading(false)
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
      setInvResult({ data: res.data })
    } catch (err: any) {
      setInvResult({ error: err?.response?.data?.detail || err.message })
    } finally {
      setInvLoading(false)
    }
  }

  const handleUploadInternal = async () => {
    if (!closingMonth || !intPaste.trim()) return
    setIntLoading(true)
    setIntResult(null)
    try {
      const res = await api.post('/api/cierre/upload-internal-inv', {
        tsv_data: intPaste,
        mes_cierre_date_id: closingMonth.date_id,
      })
      setIntResult({ data: res.data })
    } catch (err: any) {
      setIntResult({ error: err?.response?.data?.detail || err.message })
    } finally {
      setIntLoading(false)
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
      setProjResult({ data: res.data })
    } catch (err: any) {
      setProjResult({ error: err?.response?.data?.detail || err.message })
    } finally {
      setProjLoading(false)
    }
  }

  // ── UI Components ──

  const steps = [
    'Detectar mes',
    'Sales Out',
    'Sales In',
    'Inventario cliente',
    'Inventario interno',
    'Proyecciones',
  ]

  const StepIndicator = () => (
    <div className="flex items-center gap-1 mb-8 overflow-x-auto pb-2">
      {steps.map((label, i) => {
        const isActive = step === i + 1 || (i === 0 && step === 0)
        const isDone = step > i + 1 || step === 6
        return (
          <div key={i} className="flex items-center gap-1">
            <button
              onClick={() => { if (isDone || isActive) setStep(i === 0 ? 0 : i + 1) }}
              className={`flex items-center gap-1.5 px-3 py-2 rounded-xl text-xs font-medium transition-all whitespace-nowrap ${
                isActive
                  ? 'bg-tenka-accent/15 text-tenka-accent border border-tenka-accent/30'
                  : isDone
                    ? 'bg-tenka-card text-green-500 border border-green-500/20'
                    : 'text-tenka-muted border border-transparent'
              }`}
            >
              {isDone ? <CheckCircle size={14} /> : <Circle size={14} />}
              {label}
            </button>
            {i < steps.length - 1 && <ArrowRight size={12} className="text-tenka-muted/40 flex-shrink-0" />}
          </div>
        )
      })}
    </div>
  )

  const ResultDisplay = ({ result, label }: { result: StepResult | null; label: string }) => {
    if (!result) return null
    if (result.error) return (
      <div className="mt-4 bg-red-500/10 border border-red-500/20 rounded-xl p-4">
        <div className="flex items-center gap-2 text-red-400 text-sm font-medium mb-1">
          <AlertTriangle size={14} /> Error
        </div>
        <p className="text-red-300 text-xs">{result.error}</p>
      </div>
    )
    return (
      <div className="mt-4 bg-green-500/10 border border-green-500/20 rounded-xl p-4">
        <div className="flex items-center gap-2 text-green-400 text-sm font-medium mb-2">
          <CheckCircle size={14} /> {label}
        </div>
        <pre className="text-xs text-tenka-muted overflow-auto max-h-48">
          {JSON.stringify(result.data, null, 2)}
        </pre>
      </div>
    )
  }

  const NavButtons = ({ onBack, onNext, nextLabel, nextDisabled }: {
    onBack?: () => void
    onNext?: () => void
    nextLabel?: string
    nextDisabled?: boolean
  }) => (
    <div className="flex justify-between items-center mt-6">
      {onBack ? (
        <button onClick={onBack} className="flex items-center gap-1 text-tenka-muted hover:text-tenka-text text-sm">
          <ArrowLeft size={14} /> Anterior
        </button>
      ) : <div />}
      {onNext && (
        <button
          onClick={onNext}
          disabled={nextDisabled}
          className="flex items-center gap-1 bg-tenka-accent hover:bg-tenka-accent/80 disabled:opacity-50 text-white px-5 py-2.5 rounded-xl text-sm font-medium transition-all"
        >
          {nextLabel || 'Siguiente'} <ArrowRight size={14} />
        </button>
      )}
    </div>
  )

  const PasteArea = ({ value, onChange, placeholder }: {
    value: string
    onChange: (v: string) => void
    placeholder: string
  }) => (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      rows={10}
      className="w-full bg-tenka-surface border border-tenka-border rounded-xl p-4 text-sm font-mono text-tenka-text placeholder-tenka-muted/50 focus:outline-none focus:border-tenka-accent/50 resize-y"
    />
  )

  // ── Render ──

  return (
    <div className="max-w-3xl">
      <h1 className="text-3xl font-bold text-tenka-text mb-2">Cierre de Mes</h1>
      <p className="text-tenka-muted text-sm mb-6">
        Wizard guiado para cerrar el mes, cargar datos y ejecutar proyecciones.
      </p>

      {step > 0 && step < 6 && <StepIndicator />}

      {/* ═══ Step 0: Landing ═══ */}
      {step === 0 && (
        <div className="max-w-md">
          <div className="bg-tenka-card rounded-2xl border border-tenka-border p-6">
            {closingLoading && (
              <div className="flex items-center gap-2 text-tenka-muted">
                <Loader2 className="animate-spin" size={16} /> Detectando mes de cierre...
              </div>
            )}
            {closingError && (
              <div className="text-red-400 text-sm">
                <AlertTriangle size={14} className="inline mr-1" />
                {closingError}
              </div>
            )}
            {closingMonth && (
              <>
                <p className="text-sm text-tenka-muted mb-1">Último mes con datos:</p>
                <p className="text-lg font-semibold text-tenka-text mb-4">{closingMonth.last_data_month}</p>
                <p className="text-sm text-tenka-muted mb-1">Mes a cerrar:</p>
                <p className="text-3xl font-bold text-tenka-accent">{closingMonth.label}</p>
              </>
            )}
          </div>
          {closingMonth && (
            <button
              onClick={() => setStep(1)}
              className="mt-6 w-full bg-tenka-accent hover:bg-tenka-accent/80 text-white px-6 py-3 rounded-xl text-sm font-semibold transition-all flex items-center justify-center gap-2"
            >
              <Play size={16} /> Iniciar Cierre
            </button>
          )}
        </div>
      )}

      {/* ═══ Step 1: Upload Sales Out ═══ */}
      {step === 1 && (
        <div>
          <h2 className="text-lg font-semibold text-tenka-text mb-1">Paso 1: Cargar Sales Out</h2>
          <p className="text-sm text-tenka-muted mb-4">
            Pegue los datos de sell-out (ventas al consumidor) desde su hoja de cálculo.
          </p>
          <PasteArea
            value={soPaste}
            onChange={setSoPaste}
            placeholder={"Fecha Final\tCliente\tUPC\tUnidades\n2025-08-31\tCLIENTE A\t7501234567890\t150\n2025-08-31\tCLIENTE B\t7501234567891\t230"}
          />
          <button
            onClick={handleUploadSO}
            disabled={soLoading || !soPaste.trim()}
            className="mt-3 w-full bg-tenka-accent2 hover:bg-tenka-accent2/80 disabled:opacity-50 text-white px-6 py-3 rounded-xl text-sm font-semibold transition-all flex items-center justify-center gap-2"
          >
            {soLoading ? <Loader2 className="animate-spin" size={16} /> : <ClipboardPaste size={16} />}
            Cargar Sales Out
          </button>
          <ResultDisplay result={soResult} label="Sales Out cargado correctamente" />
          <NavButtons
            onBack={() => setStep(0)}
            onNext={() => setStep(2)}
            nextDisabled={!soResult?.data}
          />
        </div>
      )}

      {/* ═══ Step 2: Upload Sales In ═══ */}
      {step === 2 && (
        <div>
          <h2 className="text-lg font-semibold text-tenka-text mb-1">Paso 2: Cargar Sales In</h2>
          <p className="text-sm text-tenka-muted mb-4">
            Pegue los datos de sell-in (ventas de Tenka al cliente) desde su hoja de cálculo.
          </p>
          <PasteArea
            value={siPaste}
            onChange={setSiPaste}
            placeholder={"Fecha Final\tCliente\tUPC\tUnidades\n2025-08-31\tCLIENTE A\t7501234567890\t200\n2025-08-31\tCLIENTE B\t7501234567891\t180"}
          />
          <button
            onClick={handleUploadSI}
            disabled={siLoading || !siPaste.trim()}
            className="mt-3 w-full bg-tenka-accent2 hover:bg-tenka-accent2/80 disabled:opacity-50 text-white px-6 py-3 rounded-xl text-sm font-semibold transition-all flex items-center justify-center gap-2"
          >
            {siLoading ? <Loader2 className="animate-spin" size={16} /> : <ClipboardPaste size={16} />}
            Cargar Sales In
          </button>
          <ResultDisplay result={siResult} label="Sales In cargado correctamente" />
          <NavButtons
            onBack={() => setStep(1)}
            onNext={() => setStep(3)}
            nextDisabled={!siResult?.data}
          />
        </div>
      )}

      {/* ═══ Step 3: Calculate client inventory ═══ */}
      {step === 3 && (
        <div>
          <h2 className="text-lg font-semibold text-tenka-text mb-1">Paso 3: Calcular inventario de cliente</h2>
          <p className="text-sm text-tenka-muted mb-4">
            Calcula el inventario final de cada cliente-SKU para {closingMonth?.label}. Si resulta negativo, aplica corrección retrospectiva automática.
          </p>
          <div className="bg-tenka-card rounded-2xl border border-tenka-border p-6">
            <button
              onClick={handleCalcInventory}
              disabled={invLoading}
              className="w-full bg-tenka-accent2 hover:bg-tenka-accent2/80 disabled:opacity-50 text-white px-6 py-3 rounded-xl text-sm font-semibold transition-all flex items-center justify-center gap-2"
            >
              {invLoading ? <Loader2 className="animate-spin" size={16} /> : <Package size={16} />}
              Calcular inventario
            </button>
            <ResultDisplay result={invResult} label="Inventario calculado" />
          </div>
          <NavButtons
            onBack={() => setStep(2)}
            onNext={() => setStep(4)}
            nextDisabled={!invResult?.data}
          />
        </div>
      )}

      {/* ═══ Step 4: Upload internal inventory ═══ */}
      {step === 4 && (
        <div>
          <h2 className="text-lg font-semibold text-tenka-text mb-1">Paso 4: Cargar inventario interno</h2>
          <p className="text-sm text-tenka-muted mb-4">
            Pegue el inventario interno de Tenka al cierre del mes.
          </p>
          <PasteArea
            value={intPaste}
            onChange={setIntPaste}
            placeholder={"UPC\tInventario Cierre de Mes\n7501234567890\t500\n7501234567891\t1200\n7501234567892\t340"}
          />
          <button
            onClick={handleUploadInternal}
            disabled={intLoading || !intPaste.trim()}
            className="mt-3 w-full bg-tenka-accent2 hover:bg-tenka-accent2/80 disabled:opacity-50 text-white px-6 py-3 rounded-xl text-sm font-semibold transition-all flex items-center justify-center gap-2"
          >
            {intLoading ? <Loader2 className="animate-spin" size={16} /> : <ClipboardPaste size={16} />}
            Cargar inventario interno
          </button>
          <ResultDisplay result={intResult} label="Inventario interno cargado" />
          <NavButtons
            onBack={() => setStep(3)}
            onNext={() => setStep(5)}
            nextDisabled={!intResult?.data}
          />
        </div>
      )}

      {/* ═══ Step 5: Run projections ═══ */}
      {step === 5 && (
        <div>
          <h2 className="text-lg font-semibold text-tenka-text mb-1">Paso 5: Ejecutar proyecciones</h2>
          <p className="text-sm text-tenka-muted mb-4">
            Ejecuta el proceso mensual completo (Unconstrained + Constrained Demand) para {closingMonth?.label}.
          </p>
          <div className="bg-tenka-card rounded-2xl border border-tenka-border p-6">
            <button
              onClick={handleRunProjections}
              disabled={projLoading}
              className="w-full bg-tenka-accent2 hover:bg-tenka-accent2/80 disabled:opacity-50 text-white px-6 py-3 rounded-xl text-sm font-semibold transition-all flex items-center justify-center gap-2"
            >
              {projLoading ? <Loader2 className="animate-spin" size={16} /> : <Play size={16} />}
              Ejecutar proyecciones
            </button>
            <ResultDisplay result={projResult} label="Proyecciones completadas" />
            {projResult?.data && (
              <button
                onClick={() => setStep(6)}
                className="mt-4 w-full bg-green-600 hover:bg-green-600/80 text-white px-6 py-3 rounded-xl text-sm font-semibold transition-all flex items-center justify-center gap-2"
              >
                <CheckCircle size={16} /> Finalizar cierre
              </button>
            )}
          </div>
          <NavButtons onBack={() => setStep(4)} />
        </div>
      )}

      {/* ═══ Step 6: Done ═══ */}
      {step === 6 && (
        <div className="max-w-md text-center mx-auto">
          <div className="bg-green-500/10 border border-green-500/20 rounded-2xl p-8">
            <CheckCircle size={48} className="text-green-400 mx-auto mb-4" />
            <h2 className="text-2xl font-bold text-tenka-text mb-2">Cierre completado</h2>
            <p className="text-tenka-muted text-sm mb-6">
              El mes <span className="font-semibold text-tenka-accent">{closingMonth?.label}</span> se cerró exitosamente. Las proyecciones Unconstrained y Constrained están disponibles en Reportes.
            </p>
            <div className="bg-tenka-surface rounded-xl p-4 text-left text-xs text-tenka-muted space-y-1 mb-6">
              <p><span className="text-tenka-text font-medium">Sales Out:</span> {soResult?.data?.inserted ?? 0} registros</p>
              <p><span className="text-tenka-text font-medium">Sales In:</span> {siResult?.data?.inserted ?? 0} registros</p>
              <p><span className="text-tenka-text font-medium">Inventario cliente:</span> {invResult?.data?.calculated ?? 0} pares calculados, {invResult?.data?.corrections ?? 0} correcciones</p>
              <p><span className="text-tenka-text font-medium">Inventario interno:</span> {intResult?.data?.inserted ?? 0} SKUs</p>
            </div>
            <button
              onClick={() => navigate('/reportes')}
              className="w-full bg-tenka-accent hover:bg-tenka-accent/80 text-white px-6 py-3 rounded-xl text-sm font-semibold transition-all"
            >
              Ir a Reportes
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
