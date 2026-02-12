import { NavLink } from 'react-router-dom'
import { BarChart3, CalendarCheck, UserPlus, Settings } from 'lucide-react'

const links = [
  { to: '/reportes', label: 'Reportes', icon: BarChart3 },
  { to: '/cierre', label: 'Cierre de mes', icon: CalendarCheck },
  { to: '/altas-bajas', label: 'Altas, bajas y mod.', icon: UserPlus },
  { to: '/configuracion', label: 'Configuracion', icon: Settings },
]

export default function Sidebar() {
  return (
    <aside className="w-64 min-h-screen bg-tenka-surface border-r border-tenka-border flex flex-col">
      {/* Logo */}
      <div className="px-6 py-6 border-b border-tenka-border">
        <h1 className="text-2xl font-bold bg-gradient-to-r from-tenka-accent to-tenka-accent2 bg-clip-text text-transparent">
          Tenka
        </h1>
        <p className="text-xs text-tenka-muted mt-1">Control de Inventarios</p>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-3 py-4 space-y-1">
        {links.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              `flex items-center gap-3 px-4 py-3 rounded-xl text-sm font-medium transition-all duration-200 ${
                isActive
                  ? 'bg-tenka-accent/15 text-tenka-accent border border-tenka-accent/30'
                  : 'text-tenka-muted hover:text-tenka-text hover:bg-tenka-card border border-transparent'
              }`
            }
          >
            <Icon size={18} />
            {label}
          </NavLink>
        ))}
      </nav>

      {/* Footer */}
      <div className="px-6 py-4 border-t border-tenka-border">
        <p className="text-xs text-tenka-muted">Staging Demo v1.0</p>
        <p className="text-xs text-tenka-muted/60 mt-0.5">Mes cierre: 202507</p>
      </div>
    </aside>
  )
}
