import { useState, useCallback } from 'react'
import { Upload, CheckCircle, XCircle, Loader2 } from 'lucide-react'
import api from '../api/client'

interface Props {
  label: string
  endpoint: string
  description: string
}

type Status = 'idle' | 'uploading' | 'success' | 'error'

export default function FileUploadCard({ label, endpoint, description }: Props) {
  const [status, setStatus] = useState<Status>('idle')
  const [message, setMessage] = useState('')
  const [dragOver, setDragOver] = useState(false)

  const upload = useCallback(async (file: File) => {
    setStatus('uploading')
    setMessage('')
    const formData = new FormData()
    formData.append('file', file)
    try {
      const res = await api.post(endpoint, formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      setStatus('success')
      setMessage(res.data?.message || 'Cargado exitosamente')
    } catch (err: any) {
      setStatus('error')
      setMessage(err?.response?.data?.detail || err.message || 'Error al cargar')
    }
  }, [endpoint])

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    const file = e.dataTransfer.files[0]
    if (file) upload(file)
  }, [upload])

  const handleInput = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) upload(file)
  }, [upload])

  const statusIcon = {
    idle: <Upload size={24} className="text-tenka-muted" />,
    uploading: <Loader2 size={24} className="text-tenka-accent animate-spin" />,
    success: <CheckCircle size={24} className="text-tenka-success" />,
    error: <XCircle size={24} className="text-tenka-danger" />,
  }

  const borderColor = {
    idle: dragOver ? 'border-tenka-accent' : 'border-tenka-border',
    uploading: 'border-tenka-accent',
    success: 'border-tenka-success/50',
    error: 'border-tenka-danger/50',
  }

  return (
    <div
      className={`bg-tenka-card rounded-2xl border-2 border-dashed p-5 text-center transition-all duration-300 cursor-pointer hover:bg-tenka-surface/50 ${borderColor[status]}`}
      onDrop={handleDrop}
      onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
      onDragLeave={() => setDragOver(false)}
      onClick={() => document.getElementById(`file-${endpoint}`)?.click()}
    >
      <input id={`file-${endpoint}`} type="file" accept=".xlsx,.xls" onChange={handleInput} className="hidden" />
      <div className="mb-3">{statusIcon[status]}</div>
      <p className="text-sm font-medium text-tenka-text">{label}</p>
      <p className="text-xs text-tenka-muted mt-1">{description}</p>
      {message && (
        <p className={`text-xs mt-2 ${status === 'error' ? 'text-tenka-danger' : 'text-tenka-success'}`}>
          {message}
        </p>
      )}
    </div>
  )
}
