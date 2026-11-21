/// <reference types="vitest" />
import { defineConfig, type UserConfig } from 'vite';
import preact from '@preact/preset-vite';
type TestConfig = UserConfig & {
  test: {
    environment: string;
    setupFiles: string;
    globals: boolean;
    include: string[];
  };
};
export default defineConfig({
  plugins: [preact()],
  base: './',
  build: { outDir: 'dist', target: 'es2021' },
  server: { host: '127.0.0.1', port: 1420, strictPort: true },
  test: {
    environment: 'happy-dom',
    setupFiles: './src/test/setup.ts',
    globals: true,
    include: ['src/**/*.test.{ts,tsx}'],
  },
} as TestConfig);
