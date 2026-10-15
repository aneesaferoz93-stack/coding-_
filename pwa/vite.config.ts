import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The app is served behind an authenticated preview host during development,
// so the dev server must accept any Host header and proxy API calls to the
// FastAPI service rather than making the browser call a second origin.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    allowedHosts: true,
    hmr: { clientPort: 443, protocol: 'wss' },
    proxy: {
      '/v1': {
        target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  preview: { host: '0.0.0.0', port: 5173, allowedHosts: true },
  build: { target: 'es2020', sourcemap: false },
});
