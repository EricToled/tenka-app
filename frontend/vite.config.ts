import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
      '/upload': 'http://localhost:8000',
      '/monthly-close': 'http://localhost:8000',
      '/unconstrained': 'http://localhost:8000',
      '/constraint': 'http://localhost:8000',
      '/new-clients': 'http://localhost:8000',
      '/report': 'http://localhost:8000',
      '/init-time-dimension': 'http://localhost:8000',
      '/test': 'http://localhost:8000',
    },
  },
})
