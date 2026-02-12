import { useState, useMemo } from 'react'
import { ArrowUpDown } from 'lucide-react'

interface Column {
  key: string
  label: string
  align?: 'left' | 'right' | 'center'
  format?: (val: any) => string
}

interface Props {
  columns: Column[]
  data: Record<string, any>[]
  totalsRow?: Record<string, any> | null
  title?: string
}

function defaultFormat(val: any): string {
  if (val === null || val === undefined) return '--'
  if (typeof val === 'number') return val.toLocaleString('es-MX', { maximumFractionDigits: 1 })
  return String(val)
}

export default function DataTable({ columns, data, totalsRow, title }: Props) {
  const [sortKey, setSortKey] = useState<string | null>(null)
  const [sortAsc, setSortAsc] = useState(true)

  const sorted = useMemo(() => {
    if (!sortKey) return data
    return [...data].sort((a, b) => {
      const va = a[sortKey] ?? 0
      const vb = b[sortKey] ?? 0
      if (typeof va === 'number' && typeof vb === 'number') {
        return sortAsc ? va - vb : vb - va
      }
      return sortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va))
    })
  }, [data, sortKey, sortAsc])

  const handleSort = (key: string) => {
    if (sortKey === key) {
      setSortAsc(!sortAsc)
    } else {
      setSortKey(key)
      setSortAsc(true)
    }
  }

  const alignClass = (align?: string) =>
    align === 'right' ? 'text-right' : align === 'center' ? 'text-center' : 'text-left'

  return (
    <div className="mt-4">
      {title && <h4 className="text-sm font-semibold text-tenka-muted mb-3 uppercase tracking-wider">{title}</h4>}
      <div className="overflow-x-auto rounded-xl border border-tenka-border">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-tenka-surface/80">
              {columns.map((col) => (
                <th
                  key={col.key}
                  className={`px-4 py-3 font-medium text-tenka-muted cursor-pointer hover:text-tenka-text transition-colors ${alignClass(col.align)}`}
                  onClick={() => handleSort(col.key)}
                >
                  <span className="inline-flex items-center gap-1">
                    {col.label}
                    <ArrowUpDown size={12} className="opacity-40" />
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.map((row, i) => (
              <tr
                key={i}
                className="border-t border-tenka-border/50 hover:bg-tenka-surface/40 transition-colors"
              >
                {columns.map((col) => (
                  <td key={col.key} className={`px-4 py-3 ${alignClass(col.align)}`}>
                    {col.format ? col.format(row[col.key]) : defaultFormat(row[col.key])}
                  </td>
                ))}
              </tr>
            ))}
            {totalsRow && (
              <tr className="border-t-2 border-tenka-accent/30 bg-tenka-accent/5 font-semibold">
                {columns.map((col) => (
                  <td key={col.key} className={`px-4 py-3 ${alignClass(col.align)}`}>
                    {col.format ? col.format(totalsRow[col.key]) : defaultFormat(totalsRow[col.key])}
                  </td>
                ))}
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
