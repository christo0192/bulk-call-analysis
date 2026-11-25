import { defineConfig, devices } from '@playwright/test';

// Another worktree's dev server on the same port would be silently reused, so tests could run
// against the wrong code; set UI_PORT to keep them apart.
const port = Number(process.env.UI_PORT ?? 1420);
export default defineConfig({
  testDir: './e2e',
  use: { ...devices['Desktop Chrome'], baseURL: `http://127.0.0.1:${port}` },
  webServer: {
    command: 'npm run dev',
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: !process.env.CI,
    env: { VITE_BRIDGE: 'mock', UI_PORT: String(port) },
  },
});
