import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],

  optimizeDeps: {
    // maplibre-gl ships its own web worker and resolves it relative to its own
    // module URL. Vite's dependency pre-bundler rewrites the entry but does
    // not emit the worker alongside it, so the worker 404s and the Map
    // constructor throws. Excluding it leaves the package to be served as
    // authored, which is what its worker resolution assumes.
    exclude: ['maplibre-gl'],
  },
})
