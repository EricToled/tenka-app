import { Routes, Route, Navigate } from 'react-router-dom'
import MainLayout from './layouts/MainLayout'
import ReportesPage from './pages/ReportesPage'
import CierreMesPage from './pages/CierreMesPage'
import AltasBajasPage from './pages/AltasBajasPage'
import ConfiguracionPage from './pages/ConfiguracionPage'
import ConstraintPage from './pages/ConstraintPage'

function App() {
  return (
    <MainLayout>
      <Routes>
        <Route path="/" element={<Navigate to="/cierre" replace />} />
        <Route path="/cierre" element={<CierreMesPage />} />
        <Route path="/reportes" element={<ReportesPage />} />
        <Route path="/altas-bajas" element={<AltasBajasPage />} />
        <Route path="/constraint" element={<ConstraintPage />} />
        <Route path="/configuracion" element={<ConfiguracionPage />} />
      </Routes>
    </MainLayout>
  )
}

export default App
