import { defineConfig, devices } from '@playwright/test'

/**
 * E2E tests require:
 *   1. Backend running: cd packages/core && uv run uvicorn src.main:app --port 8000
 *   2. Frontend (started automatically by webServer below)
 *   3. Browsers installed: npx playwright install chromium
 */

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: 'http://localhost:3001',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  // Start Next.js dev server automatically before running E2E tests.
  // Use port 3001 to avoid conflicts with a running dev server on 3000.
  webServer: {
    command: 'npm run dev -- --port 3001',
    url: 'http://localhost:3001',
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
})
