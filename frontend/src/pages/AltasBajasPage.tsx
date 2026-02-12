import { useState, useEffect, useCallback } from 'react'
import { Check, Loader2, Edit3, Save, X } from 'lucide-react'
import api from '../api/client'
import LoadingSpinner from '../components/LoadingSpinner'
import CollapsibleBlock from '../components/CollapsibleBlock'

export default function AltasBajasPage() {
  // ── New clients/SKUs ──
  const [unregistered, setUnregistered] = useState<any[]>([])
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [loadingNew, setLoadingNew] = useState(true)
  const [registering, setRegistering] = useState(false)

  const fetchUnregistered = useCallback(async () => {
    setLoadingNew(true)
    try {
      const res = await api.get('/new-clients/unregistered')
      setUnregistered(res.data.records || [])
    } catch {}
    setLoadingNew(false)
  }, [])

  useEffect(() => { fetchUnregistered() }, [fetchUnregistered])

  const handleRegister = async () => {
    if (selected.size === 0) return
    setRegistering(true)
    try {
      await api.post('/new-clients/register', { ids: Array.from(selected) })
      setSelected(new Set())
      fetchUnregistered()
    } catch {}
    setRegistering(false)
  }

  const toggleSelect = (id: number) => {
    const s = new Set(selected)
    s.has(id) ? s.delete(id) : s.add(id)
    setSelected(s)
  }

  const toggleAll = () => {
    if (selected.size === unregistered.length) {
      setSelected(new Set())
    } else {
      setSelected(new Set(unregistered.map((r) => r.id)))
    }
  }

  // ── Clients catalog ──
  const [clients, setClients] = useState<any[]>([])
  const [loadingCli, setLoadingCli] = useState(true)
  const [editingCli, setEditingCli] = useState<number | null>(null)
  const [editVal, setEditVal] = useState('')

  useEffect(() => {
    api.get('/api/catalog/clients').then(res => { setClients(res.data); setLoadingCli(false) }).catch(() => setLoadingCli(false))
  }, [])

  const saveClient = async (id: number) => {
    try {
      await api.patch(`/api/catalog/clients/${id}`, { termino_operaciones: editVal })
      setClients(clients.map(c => c.cliente_id === id ? { ...c, termino_operaciones: editVal } : c))
      setEditingCli(null)
    } catch {}
  }

  // ── SKUs catalog ──
  const [skus, setSkus] = useState<any[]>([])
  const [loadingSku, setLoadingSku] = useState(true)
  const [editingSku, setEditingSku] = useState<number | null>(null)
  const [editSkuStatus, setEditSkuStatus] = useState('')
  const [editSkuLt, setEditSkuLt] = useState(4)

  useEffect(() => {
    api.get('/api/catalog/skus').then(res => { setSkus(res.data); setLoadingSku(false) }).catch(() => setLoadingSku(false))
  }, [])

  const saveSku = async (id: number) => {
    try {
      await api.patch(`/api/catalog/skus/${id}`, { status: editSkuStatus, lead_time_meses: editSkuLt })
      setSkus(skus.map(s => s.sku_id === id ? { ...s, status: editSkuStatus, lead_time_meses: editSkuLt } : s))
      setEditingSku(null)
    } catch {}
  }

  return (
    <div className="max-w-7xl">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-tenka-text">Altas, Bajas y Modificaciones</h1>
        <p className="text-tenka-muted mt-2">
          Gestiona clientes y SKUs nuevos, actualiza catalogos y parametros operativos
        </p>
      </div>

      <div className="space-y-6">
        {/* Section 1: New clients from forecast */}
        <CollapsibleBlock title="Clientes/SKUs nuevos desde forecast" subtitle="Pares detectados en el forecast sin datos historicos">
          {loadingNew ? <LoadingSpinner /> : (
            <>
              {unregistered.length === 0 ? (
                <p className="text-sm text-tenka-muted py-4">No hay registros pendientes de alta</p>
              ) : (
                <>
                  <div className="overflow-x-auto rounded-xl border border-tenka-border mt-2">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="bg-tenka-surface/80">
                          <th className="px-4 py-3 text-left">
                            <input type="checkbox" checked={selected.size === unregistered.length} onChange={toggleAll}
                              className="rounded border-tenka-border" />
                          </th>
                          <th className="px-4 py-3 text-left text-tenka-muted font-medium">ID</th>
                          <th className="px-4 py-3 text-left text-tenka-muted font-medium">Cliente</th>
                          <th className="px-4 py-3 text-left text-tenka-muted font-medium">UPC</th>
                          <th className="px-4 py-3 text-left text-tenka-muted font-medium">Descripcion</th>
                          <th className="px-4 py-3 text-left text-tenka-muted font-medium">Tipo</th>
                          <th className="px-4 py-3 text-left text-tenka-muted font-medium">Comentario</th>
                        </tr>
                      </thead>
                      <tbody>
                        {unregistered.map((r) => (
                          <tr key={r.id} className="border-t border-tenka-border/50 hover:bg-tenka-surface/40 transition-colors">
                            <td className="px-4 py-3">
                              <input type="checkbox" checked={selected.has(r.id)} onChange={() => toggleSelect(r.id)}
                                className="rounded border-tenka-border" />
                            </td>
                            <td className="px-4 py-3 text-tenka-muted">{r.id}</td>
                            <td className="px-4 py-3">{r.cliente_nombre}</td>
                            <td className="px-4 py-3 font-mono text-xs">{r.upc}</td>
                            <td className="px-4 py-3">{r.descripcion}</td>
                            <td className="px-4 py-3">
                              <span className="px-2 py-0.5 rounded-full text-xs bg-tenka-warning/15 text-tenka-warning">
                                {r.tipo_problema}
                              </span>
                            </td>
                            <td className="px-4 py-3 text-xs text-tenka-muted">{r.comentario}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div className="mt-4 flex items-center gap-3">
                    <button
                      onClick={handleRegister}
                      disabled={registering || selected.size === 0}
                      className="bg-tenka-success hover:bg-tenka-success/80 disabled:opacity-50 text-white px-5 py-2 rounded-xl text-sm font-medium transition-all flex items-center gap-2"
                    >
                      {registering ? <Loader2 className="animate-spin" size={14} /> : <Check size={14} />}
                      Marcar como dado de alta ({selected.size})
                    </button>
                  </div>
                </>
              )}
            </>
          )}
        </CollapsibleBlock>

        {/* Section 2: Client catalog */}
        <CollapsibleBlock title="Catalogo de Clientes" subtitle="Edita termino de operaciones">
          {loadingCli ? <LoadingSpinner /> : (
            <div className="overflow-x-auto rounded-xl border border-tenka-border mt-2">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-tenka-surface/80">
                    <th className="px-4 py-3 text-left text-tenka-muted font-medium">Cliente</th>
                    <th className="px-4 py-3 text-left text-tenka-muted font-medium">Canal</th>
                    <th className="px-4 py-3 text-left text-tenka-muted font-medium">Region</th>
                    <th className="px-4 py-3 text-left text-tenka-muted font-medium">Status</th>
                    <th className="px-4 py-3 text-left text-tenka-muted font-medium">Termino Op.</th>
                    <th className="px-4 py-3 text-left text-tenka-muted font-medium">Acciones</th>
                  </tr>
                </thead>
                <tbody>
                  {clients.map((c) => (
                    <tr key={c.cliente_id} className="border-t border-tenka-border/50 hover:bg-tenka-surface/40 transition-colors">
                      <td className="px-4 py-3 font-medium">{c.cliente_nombre}</td>
                      <td className="px-4 py-3 text-tenka-muted">{c.canal || '--'}</td>
                      <td className="px-4 py-3 text-tenka-muted">{c.region || '--'}</td>
                      <td className="px-4 py-3">{c.status}</td>
                      <td className="px-4 py-3">
                        {editingCli === c.cliente_id ? (
                          <input
                            type="text" value={editVal} onChange={(e) => setEditVal(e.target.value)}
                            className="bg-tenka-surface border border-tenka-accent rounded px-2 py-1 text-sm w-32"
                          />
                        ) : (
                          <span className={c.termino_operaciones === 'Activo' ? 'text-tenka-success' : 'text-tenka-warning'}>
                            {c.termino_operaciones}
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        {editingCli === c.cliente_id ? (
                          <div className="flex gap-1">
                            <button onClick={() => saveClient(c.cliente_id)} className="text-tenka-success hover:text-tenka-success/80"><Save size={14} /></button>
                            <button onClick={() => setEditingCli(null)} className="text-tenka-muted hover:text-tenka-danger"><X size={14} /></button>
                          </div>
                        ) : (
                          <button onClick={() => { setEditingCli(c.cliente_id); setEditVal(c.termino_operaciones || '') }}
                            className="text-tenka-muted hover:text-tenka-accent"><Edit3 size={14} /></button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CollapsibleBlock>

        {/* Section 3: SKU catalog */}
        <CollapsibleBlock title="Catalogo de SKUs" subtitle="Edita status y lead time" defaultOpen={false}>
          {loadingSku ? <LoadingSpinner /> : (
            <div className="overflow-x-auto rounded-xl border border-tenka-border mt-2">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-tenka-surface/80">
                    <th className="px-4 py-3 text-left text-tenka-muted font-medium">UPC</th>
                    <th className="px-4 py-3 text-left text-tenka-muted font-medium">Descripcion</th>
                    <th className="px-4 py-3 text-left text-tenka-muted font-medium">Familia</th>
                    <th className="px-4 py-3 text-left text-tenka-muted font-medium">Status</th>
                    <th className="px-4 py-3 text-left text-tenka-muted font-medium">LT (meses)</th>
                    <th className="px-4 py-3 text-left text-tenka-muted font-medium">Acciones</th>
                  </tr>
                </thead>
                <tbody>
                  {skus.map((s) => (
                    <tr key={s.sku_id} className="border-t border-tenka-border/50 hover:bg-tenka-surface/40 transition-colors">
                      <td className="px-4 py-3 font-mono text-xs">{s.upc}</td>
                      <td className="px-4 py-3">{s.sku_descripcion}</td>
                      <td className="px-4 py-3 text-tenka-muted">{s.familia}</td>
                      <td className="px-4 py-3">
                        {editingSku === s.sku_id ? (
                          <select value={editSkuStatus} onChange={(e) => setEditSkuStatus(e.target.value)}
                            className="bg-tenka-surface border border-tenka-accent rounded px-2 py-1 text-sm">
                            <option value="Activo">Activo</option>
                            <option value="Baja">Baja</option>
                          </select>
                        ) : (
                          <span className={s.status === 'Baja' ? 'text-tenka-danger' : 'text-tenka-success'}>{s.status}</span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        {editingSku === s.sku_id ? (
                          <input type="number" value={editSkuLt} onChange={(e) => setEditSkuLt(Number(e.target.value))}
                            className="bg-tenka-surface border border-tenka-accent rounded px-2 py-1 text-sm w-16" min={1} max={12} />
                        ) : s.lead_time_meses}
                      </td>
                      <td className="px-4 py-3">
                        {editingSku === s.sku_id ? (
                          <div className="flex gap-1">
                            <button onClick={() => saveSku(s.sku_id)} className="text-tenka-success hover:text-tenka-success/80"><Save size={14} /></button>
                            <button onClick={() => setEditingSku(null)} className="text-tenka-muted hover:text-tenka-danger"><X size={14} /></button>
                          </div>
                        ) : (
                          <button onClick={() => { setEditingSku(s.sku_id); setEditSkuStatus(s.status); setEditSkuLt(s.lead_time_meses) }}
                            className="text-tenka-muted hover:text-tenka-accent"><Edit3 size={14} /></button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CollapsibleBlock>
      </div>
    </div>
  )
}
