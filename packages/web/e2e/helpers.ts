import { type Page, expect } from '@playwright/test'

// Credentials used across E2E tests.
// Uses environment variables with sensible dev defaults so CI can override.
export const TEST_EMAIL = process.env.E2E_EMAIL ?? `e2e-${Date.now()}@example.com`
export const TEST_PASSWORD = process.env.E2E_PASSWORD ?? 'TestPass123!'
export const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'

/** Register a fresh user via the UI auth flow and land on /home. */
export async function registerAndLogin(page: Page, email = TEST_EMAIL, password = TEST_PASSWORD) {
  await page.goto('/auth')
  // Wait for the auth page to be fully hydrated
  await expect(page.getByPlaceholder('voce@email.com')).toBeVisible({ timeout: 15_000 })

  // Switch to "Criar conta" tab
  await page.getByRole('button', { name: 'Criar conta' }).first().click()

  // Wait for the "Nome" field — confirmation that the register tab is active
  await expect(page.getByPlaceholder('Seu nome')).toBeVisible({ timeout: 5_000 })

  await page.getByPlaceholder('Seu nome').fill('E2E User')
  await page.getByPlaceholder('voce@email.com').fill(email)
  await page.getByPlaceholder('••••••••').fill(password)
  await page.getByRole('button', { name: 'Criar conta' }).last().click()

  // Wait for redirect to /home
  await expect(page).toHaveURL(/\/home/, { timeout: 15_000 })
}

/** Login with an existing account via the UI. */
export async function login(page: Page, email = TEST_EMAIL, password = TEST_PASSWORD) {
  await page.goto('/auth')
  await expect(page.getByPlaceholder('voce@email.com')).toBeVisible({ timeout: 15_000 })
  await page.getByPlaceholder('voce@email.com').fill(email)
  await page.getByPlaceholder('••••••••').fill(password)
  await page.getByRole('button', { name: 'Entrar' }).last().click()
  await expect(page).toHaveURL(/\/home/, { timeout: 15_000 })
}

/** Wait for the streaming indicator to disappear (send button re-enabled). */
export async function waitForStreamEnd(page: Page, timeoutMs = 60_000) {
  // Proxy: the Enviar button is re-enabled once isStreaming goes false.
  await expect(page.getByRole('button', { name: 'Enviar' })).toBeEnabled({ timeout: timeoutMs })
}
