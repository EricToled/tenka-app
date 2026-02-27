/**
 * FIX BUG-02: Selector de mes ahora recibe opciones dinamicas desde la API.
 * FIX BUG-01: Los filtros de cliente y familia ya se envian al API (ver ReportesPage).
 */

interface MonthOption {
  date_id: number
  label: string
}

interface Props {
  mesCierre: string
  setMesCierre: (v: string) => void
  cliente: string
  setCliente: (v: string) => void
  familia: string
  setFamilia: (v: string) => void
  clientes: string[]
  familias: string[]
  meses: MonthOption[]
  onRefresh: () => void
  loading?: boolean
}

export default function FilterBar({
  mesCierre, setMesCierre,
  cliente, setCliente,
  familia, setFamilia,
  clientes, familias, meses,
  onRefresh, loading,
}: Props) {
  const selectClass =
    'bg-tenka-surface border border-tenka-border rounded-xl px-4 py-2.5 text-sm text-tenka-text focus:outline-none focus:border-tenka-accent transition-colors'

  return (
    <div className="flex flex-wrap items-center gap-3 mb-6">
      <select value={mesCierre} onChange={(e) => setMesCierre(e.target.value)} className={selectClass}>
        {meses.length > 0 ? (
          meses.map((m) => (
            <option key={m.date_id} value={String(m.date_id)}>{m.label}</option>
          ))
        ) : (
          <option value={mesCierre}>{mesCierre}</option>
        )}
      </select>

      <select value={cliente} onChange={(e) => setCliente(e.target.value)} className={selectClass}>
        <option value="">Todos los clientes</option>
        {clientes.map((c) => (
          <option key={c} value={c}>{c}</option>
        ))}
      </select>

      <select value={familia} onChange={(e) => setFamilia(e.target.value)} className={selectClass}>
        <option value="">Todas las familias</option>
        {familias.map((f) => (
          <option key={f} value={f}>{f}</option>
        ))}
      </select>

      <button
        onClick={onRefresh}
        disabled={loading}
        className="bg-tenka-accent hover:bg-tenka-accent/80 disabled:opacity-50 text-white px-6 py-2.5 rounded-xl text-sm font-medium transition-all duration-200 hover:scale-105"
      >
        {loading ? 'Cargando...' : 'Actualizar reportes'}
      </button>
    </div>
  )
}
