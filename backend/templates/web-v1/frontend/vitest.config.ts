import { defineConfig } from 'vitest/config';
import { fileURLToPath, URL } from 'node:url';
export default defineConfig({ resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } }, test: { environment: 'jsdom', setupFiles: ['./src/test-setup.ts'], restoreMocks: true, pool: 'threads', maxWorkers: 2, minWorkers: 1, fileParallelism: false } });
