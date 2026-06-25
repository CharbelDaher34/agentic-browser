import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server proxies API + artifacts + WebSockets to the FastAPI backend on :8000,
// so the frontend can use same-origin relative URLs in dev and in production
// (where FastAPI serves the built dist).
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/artifacts': { target: 'http://localhost:8000', changeOrigin: true },
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
})
