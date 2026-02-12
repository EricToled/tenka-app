import { Routes, Route, Navigate } from 'react-router-dom'
import MainLayout from './layouts/MainLayout'
import ReportesPage from './pages/ReportesPage'
import CierreMesPage from './pages/CierreMesPage'
import AltasBajasPage from './pages/AltasBajasPage'
import ConfiguracionPage from './pages/ConfiguracionPage'

function App() {
  return (
    <MainLayout>
      <Routes>
        <Route path="/" element={<Navigate to="/reportes" replace />} />
        <Route path="/reportes" element={<ReportesPage />} />
        <Route path="/cierre" element={<CierreMesPage />} />
        <Route path="/altas-bajas" element={<AltasBajasPage />} />
        <Route path="/configuracion" element={<ConfiguracionPage />} />
      </Routes>
    </MainLayout>
  )
}

export default App
