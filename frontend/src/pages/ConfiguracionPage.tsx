import { useState, useEffect } from 'react'
import { Settings, Calendar, Package, TrendingUp, ShieldCheck, Clock, Target } from 'lucide-react'
import api from '../api/client'
import LoadingSpinner from '../components/LoadingSpinner'

interface ConfigParams {
  dim_tiempo_rango: { min_year: number | null; max_year: number | null }
  politica_cobertura_meses: number
  min_months_for_trend: number
  graduation_threshold: number
  dos_target_low: number
  dos_target_high: number
  mes_cierre_activo: number
}

function ParamCard({ icon: Icon, label, value, description, color = 'accent' }: {
  icon: React.ElementType
  label: string
  value: string | number
  description: string
  color?: 'accent' | 'success' | 'warning' | 'accent2' | 'danger'
}) {
  const colors = {
    accent: 'border-tenka-accent/30 bg-tenka-accent/5',
    success: 'border-tenka-success/30 bg-tenka-success/5',
    warning: 'border-tenka-warning/30 bg-tenka-warning/5',
    accent2: 'border-tenka-accent2/30 bg-tenka-accent2/5',
    danger: 'border-tenka-danger/30 bg-tenka-danger/5',
  }
  const iconColors = {
    accent: 'text-tenka-accent',
    success: 'text-tenka-success',
    warning: 'text-tenka-warning',
    accent2: 'text-tenka-accent2',
    danger: 'text-tenka-danger',
  }

  return (
    <div className={`rounded-2xl border p-5 ${colors[color]} transition-all duration-300 hover:scale-[1.01]`}>
      <div className="flex items-start gap-4">
        <div className={`p-2.5 rounded-xl bg-tenka-surface/80 ${iconColors[color]}`}>
          <Icon size={22} />
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-xs font-medium text-tenka-muted uppercase tracking-wider mb-1">{label}</p>
          <p className="text-xl font-bold text-tenka-text">{value}</p>
          <p className="text-xs text-tenka-muted mt-1.5 leading-relaxed">{description}</p>
        </div>
      </div>
    </div>
  )
}

export default function ConfiguracionPage() {
  const [config, setConfig] = useState<ConfigParams | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.get('/api/config/params')
      .then(res => { setConfig(res.data); setLoading(false) })
      .catch(() => setLoading(false))
  }, [])

  if (loading) return <LoadingSpinner />

  if (!config) {
    return (
      <div className="max-w-7xl">
        <h1 className="text-3xl font-bold text-tenka-text mb-4">Configuracion</h1>
        <p className="text-tenka-muted">No se pudieron cargar los parametros de configuracion.</p>
      </div>
    )
  }

  const mesCierre = config.mes_cierre_activo
  const mesCierreStr = `${String(mesCierre).slice(4)}/${String(mesCierre).slice(0, 4)}`

  return (
    <div className="max-w-7xl">
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-tenka-text">Configuracion</h1>
        <p className="text-tenka-muted mt-2">
          Parametros operativos del sistema (solo lectura)
        </p>
      </div>

      {/* Section: General */}
      <div className="bg-tenka-card rounded-2xl border border-tenka-border p-6 mb-6">
        <div className="flex items-center gap-3 mb-5">
          <Settings size={20} className="text-tenka-accent" />
          <h2 className="text-lg font-semibold text-tenka-text">Parametros Generales</h2>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          <ParamCard
            icon={Calendar}
            label="Mes de Cierre Activo"
            value={mesCierreStr}
            description={`El cierre actual es el mes ${mesCierre}. Los 12 meses proyectados parten de este punto.`}
            color="accent"
          />
          <ParamCard
            icon={Calendar}
            label="Rango dim_tiempo"
            value={
              config.dim_tiempo_rango.min_year && config.dim_tiempo_rango.max_year
                ? `${config.dim_tiempo_rango.min_year} — ${config.dim_tiempo_rango.max_year}`
                : 'Sin datos'
            }
            description="Rango de anos disponibles en la dimension de tiempo."
            color="accent2"
          />
          <ParamCard
            icon={Package}
            label="Politica de Cobertura"
            value={`${config.politica_cobertura_meses} meses`}
            description="Lead time default de inventario interno (L). Las POs se generan para cubrir L meses de demanda futura."
            color="success"
          />
        </div>
      </div>

      {/* Section: Statistical Model */}
      <div className="bg-tenka-card rounded-2xl border border-tenka-border p-6 mb-6">
        <div className="flex items-center gap-3 mb-5">
          <TrendingUp size={20} className="text-tenka-accent2" />
          <h2 className="text-lg font-semibold text-tenka-text">Modelo Estadistico</h2>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          <ParamCard
            icon={TrendingUp}
            label="Meses Minimos para Tendencia"
            value={`${config.min_months_for_trend} meses`}
            description="Si un par cliente-SKU tiene menos de este umbral de meses con ventas, se usa proyeccion plana en lugar de tendencia lineal."
            color="accent2"
          />
          <ParamCard
            icon={ShieldCheck}
            label="Umbral de Graduacion"
            value={`${config.graduation_threshold} meses`}
            description="Meses minimos de Sales Out real para que un cliente nuevo 'gradúe' al modelo estadistico. Antes de graduarse, usa forecast como base."
            color="warning"
          />
        </div>
      </div>

      {/* Section: DOS Targets */}
      <div className="bg-tenka-card rounded-2xl border border-tenka-border p-6 mb-6">
        <div className="flex items-center gap-3 mb-5">
          <Target size={20} className="text-tenka-success" />
          <h2 className="text-lg font-semibold text-tenka-text">Objetivos de Dias de Inventario (DOS)</h2>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          <ParamCard
            icon={Target}
            label="DOS Objetivo Bajo"
            value={`${config.dos_target_low} dias`}
            description="Meta final de dias de inventario. Si DOS actual esta entre 60-90, se hace convergencia lineal en 12 meses."
            color="success"
          />
          <ParamCard
            icon={Clock}
            label="DOS Limite Alto"
            value={`${config.dos_target_high} dias`}
            description="Si DOS actual > 90, primero se baja a 90 en 6 meses, luego a 60 en los siguientes 6 meses."
            color="danger"
          />
        </div>
      </div>

      {/* Section: Info */}
      <div className="bg-tenka-surface/50 rounded-2xl border border-tenka-border/50 p-5">
        <p className="text-xs text-tenka-muted leading-relaxed">
          <span className="text-tenka-accent font-medium">Nota:</span> Estos parametros son de solo lectura
          en esta interfaz. Para modificarlos, contacta al equipo de desarrollo. Los cambios en politica
          de cobertura o umbrales de tendencia afectan directamente las proyecciones de demanda y la
          generacion de ordenes de compra.
        </p>
      </div>
    </div>
  )
}
