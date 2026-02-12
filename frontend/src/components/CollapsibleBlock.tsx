import { useState, type ReactNode } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'

interface Props {
  title: string
  subtitle?: string
  children: ReactNode
  defaultOpen?: boolean
}

export default function CollapsibleBlock({ title, subtitle, children, defaultOpen = true }: Props) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <div className="bg-tenka-card rounded-2xl border border-tenka-border overflow-hidden transition-all duration-300">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-6 py-5 hover:bg-tenka-surface/50 transition-colors"
      >
        <div className="text-left">
          <h3 className="text-lg font-semibold text-tenka-text">{title}</h3>
          {subtitle && <p className="text-sm text-tenka-muted mt-0.5">{subtitle}</p>}
        </div>
        <span className="text-tenka-muted ml-4">
          {open ? <ChevronDown size={20} /> : <ChevronRight size={20} />}
        </span>
      </button>
      {open && <div className="px-6 pb-6">{children}</div>}
    </div>
  )
}
