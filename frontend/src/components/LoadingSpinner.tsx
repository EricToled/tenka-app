import { Loader2 } from 'lucide-react'

export default function LoadingSpinner({ text = 'Cargando...' }: { text?: string }) {
  return (
    <div className="flex items-center justify-center py-12 gap-3">
      <Loader2 className="animate-spin text-tenka-accent" size={24} />
      <span className="text-tenka-muted text-sm">{text}</span>
    </div>
  )
}
