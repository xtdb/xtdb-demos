import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // bind all interfaces so the dev server is reachable from outside the container
    host: true,
    proxy: { '/api': process.env.API_URL ?? 'http://localhost:8000' },
  },
})
