import { useState } from 'react'
import { Play, Loader2 } from 'lucide-react'
import api from '../api/client'

export default function ConstraintPage() {
  const [mesCierre, setMesCierre] = useState('202507')
  const [phaseAResult, setPhaseAResult] = useState<any>(null)
  const [phaseALoading, setPhaseALoading] = useState(false)
  const [phaseBResult, setPhaseBResult] = useState<any>(null)
  const [phaseBLoading, setPhaseBLoading] = useState(false)
  const [approveResult, setApproveResult] = useState<any>(null)
  const [approveLoading, setApproveLoading] = useState(false)

  const handlePhaseA = async () => {
    setPhaseALoading(true)
    setPhaseAResult(null)
    try {
      const res = await api.post(`/constraint/run/${mesCierre}`)
      setPhaseAResult(res.data)
    } catch (err: any) {
      setPhaseAResult({ error: err?.response?.data?.detail || err.message })
    } finally {
      setPhaseALoading(false)
    }
  }

  const handleApprove = async () => {
    setApproveLoading(true)
    setApproveResult(null)
    try {
      const res = await api.post(`/constraint/approve/${mesCierre}`, {})
      setApproveResult(res.data)
    } catch (err: any) {
      setApproveResult({ error: err?.response?.data?.detail || err.message })
    } finally {
      setApproveLoading(false)
    }
  }

  const handlePhaseB = async () => {
    setPhaseBLoading(true)
    setPhaseBResult(null)
    try {
      const res = await api.post(`/constraint/phase-b/${mesCierre}`)
      setPhaseBResult(res.data)
    } catch (err: any) {
      setPhaseBResult({ error: err?.response?.data?.detail || err.message })
    } finally {
      setPhaseBLoading(false)
    }
  }

  return (
    <div className="max-w-4xl">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-tenka-text">Constraint Demand</h1>
        <p className="text-tenka-muted mt-2">
          Ejecuta las fases de restriccion de inventario: POs, asignacion, aprobacion y SO constrained
        </p>
      </div>

      <div className="mb-6">
        <label className="block text-sm text-tenka-muted mb-2">Mes de cierre (YYYYMM)</label>
        <input
          type="text"
          value={mesCierre}
          onChange={(e) => setMesCierre(e.target.value)}
          className="bg-tenka-surface border border-tenka-border rounded-xl px-4 py-2.5 text-sm text-tenka-text w-48 focus:outline-none focus:border-tenka-accent"
        />
      </div>

      <div className="space-y-4">
        {/* Phase A */}
        <div className="bg-tenka-card rounded-2xl border border-tenka-border p-6">
          <h3 className="font-medium mb-2">Fase A: Inventario Interno + POs + SI Constrained</h3>
          <p className="text-sm text-tenka-muted mb-4">Genera POs, aplica restricciones de stock y calcula ventas perdidas por OOS</p>
          <button
            onClick={handlePhaseA}
            disabled={phaseALoading}
            className="bg-tenka-accent hover:bg-tenka-accent/80 disabled:opacity-50 text-white px-6 py-2.5 rounded-xl text-sm font-medium transition-all flex items-center gap-2"
          >
            {phaseALoading ? <Loader2 className="animate-spin" size={16} /> : <Play size={16} />}
            Ejecutar Fase A
          </button>
          {phaseAResult && (
            <pre className="mt-4 bg-tenka-surface rounded-xl p-4 text-xs text-tenka-muted overflow-auto max-h-48">
              {JSON.stringify(phaseAResult, null, 2)}
            </pre>
          )}
        </div>

        {/* Approve Gate */}
        <div className="bg-tenka-card rounded-2xl border border-tenka-border p-6">
          <h3 className="font-medium mb-2">Gate: Aprobar Asignacion</h3>
          <p className="text-sm text-tenka-muted mb-4">Aprueba la asignacion SI constrained para continuar a Fase B</p>
          <button
            onClick={handleApprove}
            disabled={approveLoading}
            className="bg-tenka-accent2 hover:bg-tenka-accent2/80 disabled:opacity-50 text-white px-6 py-2.5 rounded-xl text-sm font-medium transition-all flex items-center gap-2"
          >
            {approveLoading ? <Loader2 className="animate-spin" size={16} /> : <Play size={16} />}
            Aprobar y continuar
          </button>
          {approveResult && (
            <pre className="mt-4 bg-tenka-surface rounded-xl p-4 text-xs text-tenka-muted overflow-auto max-h-48">
              {JSON.stringify(approveResult, null, 2)}
            </pre>
          )}
        </div>

        {/* Phase B */}
        <div className="bg-tenka-card rounded-2xl border border-tenka-border p-6">
          <h3 className="font-medium mb-2">Fase B: SO Constrained + Inventario Cliente</h3>
          <p className="text-sm text-tenka-muted mb-4">Calcula SO constrained, inventario cliente constrained y DOS</p>
          <button
            onClick={handlePhaseB}
            disabled={phaseBLoading}
            className="bg-tenka-accent hover:bg-tenka-accent/80 disabled:opacity-50 text-white px-6 py-2.5 rounded-xl text-sm font-medium transition-all flex items-center gap-2"
          >
            {phaseBLoading ? <Loader2 className="animate-spin" size={16} /> : <Play size={16} />}
            Ejecutar Fase B
          </button>
          {phaseBResult && (
            <pre className="mt-4 bg-tenka-surface rounded-xl p-4 text-xs text-tenka-muted overflow-auto max-h-48">
              {JSON.stringify(phaseBResult, null, 2)}
            </pre>
          )}
        </div>
      </div>
    </div>
  )
}
