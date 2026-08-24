import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // The gateway runs separately in development. Proxying keeps the studio origin-clean so
      // the WebSocket and the REST calls share a host and no CORS preflight is needed at all.
      '/api': { target: 'http://127.0.0.1:8071', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8071', ws: true },
    },
  },
})
