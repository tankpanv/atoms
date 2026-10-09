import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';
const base = process.env.BASE_PATH || '/';
export default defineConfig({ plugins: [react()], base, resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } }, server: { proxy: {
  [base + 'api']: { target: `http://127.0.0.1:${process.env.API_PORT || 8000}`,
    rewrite: path => '/api' + path.slice((base + 'api').length) }
} } });
