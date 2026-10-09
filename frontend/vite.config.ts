import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    // Let /api preflight reach the API's project-aware CORS policy. Vite's
    // default CORS middleware otherwise answers opaque sandbox origins itself.
    cors: false,
    proxy: { '/api': { target: process.env.VITE_API_PROXY_TARGET || 'http://localhost:8000', changeOrigin: true, ws: true } },
  },
})
