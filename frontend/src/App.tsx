import { Routes, Route, Navigate } from 'react-router-dom'
import MainLayout from './layouts/MainLayout'
import ReportesPage from './pages/ReportesPage'
import CierreMesPage from './pages/CierreMesPage'
import AltasBajasPage from './pages/AltasBajasPage'
import ConstraintPage from './pages/ConstraintPage'

function PresupuestosPlaceholder() {
  return (
    <div className="max-w-4xl">
      <h1 className="text-3xl font-bold text-tenka-text">Presupuestos</h1>
      <p className="text-tenka-muted mt-2">Módulo en desarrollo.</p>
    </div>
  )
}

function App() {
  return (
    <MainLayout>
      <Routes>
        <Route path="/" element={<Navigate to="/reportes" replace />} />
        <Route path="/reportes" element={<ReportesPage />} />
        <Route path="/cierre" element={<CierreMesPage />} />
        <Route path="/altas-bajas" element={<AltasBajasPage />} />
        <Route path="/presupuestos" element={<PresupuestosPlaceholder />} />
        <Route path="/constraint" element={<ConstraintPage />} />
      </Routes>
    </MainLayout>
  )
}

export default App
