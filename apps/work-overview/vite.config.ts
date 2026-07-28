import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// base: './' so the built renderer loads over file:// inside the packaged app.
export default defineConfig({
  root: '.',
  base: './',
  plugins: [react()],
  server: { port: 5183, strictPort: true },
  build: { outDir: 'dist', emptyOutDir: true, sourcemap: true },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
