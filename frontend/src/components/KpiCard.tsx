import type { LucideIcon } from 'lucide-react'

interface Props {
  label: string
  value: string | number | null | undefined
  icon?: LucideIcon
  color?: 'accent' | 'success' | 'danger' | 'warning' | 'accent2'
  subtitle?: string
}

const colorMap = {
  accent: 'text-tenka-accent border-tenka-accent/30 bg-tenka-accent/10',
  accent2: 'text-tenka-accent2 border-tenka-accent2/30 bg-tenka-accent2/10',
  success: 'text-tenka-success border-tenka-success/30 bg-tenka-success/10',
  danger: 'text-tenka-danger border-tenka-danger/30 bg-tenka-danger/10',
  warning: 'text-tenka-warning border-tenka-warning/30 bg-tenka-warning/10',
}

export default function KpiCard({ label, value, icon: Icon, color = 'accent', subtitle }: Props) {
  return (
    <div className={`rounded-2xl border p-5 transition-all duration-300 hover:scale-[1.02] ${colorMap[color]}`}>
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-medium text-tenka-muted uppercase tracking-wider">{label}</span>
        {Icon && <Icon size={20} className="opacity-60" />}
      </div>
      <p className="text-2xl font-bold text-tenka-text">
        {value !== null && value !== undefined ? value.toLocaleString() : '--'}
      </p>
      {subtitle && <p className="text-xs text-tenka-muted mt-1">{subtitle}</p>}
    </div>
  )
}
