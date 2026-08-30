import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      // Forward WebSocket + HTTP calls to the FastAPI backend on :8000.
      // "ws: true" is required so the proxy handles the WebSocket upgrade.
      '/ws': { target: 'http://localhost:8000', ws: true },
      '/openapi.json': 'http://localhost:8000',
    },
  },
})
