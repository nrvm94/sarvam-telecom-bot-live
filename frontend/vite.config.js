import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      '/voice': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
      '/n8n': 'http://localhost:8000',
      // WebSocket proxy for real-time pipeline
      '/ws': {
        target: 'http://localhost:8000',
        ws: true,       // enable WebSocket proxying
        changeOrigin: true,
      },
    },
  },
})
