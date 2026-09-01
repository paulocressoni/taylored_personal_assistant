import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// M12: where the backend lives depends on WHERE vite runs.
//   - natively on the host      -> http://localhost:8000
//   - inside the Compose network -> http://backend:8000  (service name!)
// The dev overlay sets BACKEND_URL; the fallback keeps plain `npm run dev`
// working on the host. This is M08 #4 (service-name networking) in action.
const backendUrl = process.env.BACKEND_URL ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // Bind to ALL interfaces, not just 127.0.0.1 — required when the dev
    // server runs inside a container, otherwise the published port can't
    // be reached from the host browser.
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      // Forward WebSocket + HTTP calls to the backend. "ws: true" makes the
      // proxy handle the WebSocket upgrade for /ws/chat streaming.
      '/ws': { target: backendUrl, ws: true },
      '/openapi.json': backendUrl,
      '/sessions': backendUrl,
      '/health': backendUrl,
    },
    // If HMR doesn't pick up file changes through the Docker Desktop bind
    // mount, uncomment the line below to fall back to file polling:
    // watch: { usePolling: true },
  },
  test: {
    environment: 'jsdom', // browser-like globals (localStorage, etc.)
  },
})
