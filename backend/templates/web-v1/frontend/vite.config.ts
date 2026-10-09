import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';
export default defineConfig({ plugins: [react()], base: process.env.BASE_PATH || '/', resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } } });
