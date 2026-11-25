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
  // UI_PORT lets two worktrees run their browser tests side by side; the Tauri shell expects 1420.
  server: { host: '127.0.0.1', port: Number(process.env.UI_PORT ?? 1420), strictPort: true },
  test: {
    environment: 'happy-dom',
    setupFiles: './src/test/setup.ts',
    globals: true,
    include: ['src/**/*.test.{ts,tsx}'],
  },
} as TestConfig);
