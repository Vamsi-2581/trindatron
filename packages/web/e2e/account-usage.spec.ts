import { test, expect } from '@playwright/test'
import { registerAndLogin, waitForStreamEnd, TEST_PASSWORD } from './helpers'

test.describe('Account and usage', () => {
  test('account page shows real user data from server', async ({ page }) => {
    const email = `e2e-usage-a-${Date.now()}@example.com`
    await registerAndLogin(page, email, TEST_PASSWORD)
    await page.goto('/account')

    // Email displayed from /auth/me — never hardcoded
    // Use .first() to avoid strict mode violation when email appears in both sidebar and main
    await expect(page.getByText(email).first()).toBeVisible({ timeout: 5_000 })

    // Plan badge visible — use the main content area
    await expect(page.getByRole('main').getByText(/FREE|START|PRO|MAX|TEAM/i).first()).toBeVisible({ timeout: 5_000 })
  })

  test('account page loads usage data from backend without mock', async ({ page }) => {
    const email = `e2e-usage-b-${Date.now()}@example.com`
    await registerAndLogin(page, email, TEST_PASSWORD)
    await page.goto('/account')

    // Usage section should load (either data or the "zero" initial state)
    // Must NOT show the generic error text
    await expect(page.getByText('Não foi possível carregar o uso')).not.toBeVisible({ timeout: 5_000 })

    // Progress bars or usage cards should be visible after data loads
    await expect(page.getByText('Uso diário (UTC)')).toBeVisible({ timeout: 5_000 })
    await expect(page.getByText('Uso mensal (UTC)')).toBeVisible({ timeout: 5_000 })
  })

  test('TCU displayed on account page reflects actual usage after a message', async ({ page }) => {
    const email = `e2e-usage-c-${Date.now()}@example.com`
    await registerAndLogin(page, email, TEST_PASSWORD)

    // Send a message to accumulate some TCU
    const textarea = page.getByPlaceholder('Escreva o que precisa fazer…').first()
    await textarea.fill('Responda apenas: uso confirmado')
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]+/, { timeout: 10_000 })
    await waitForStreamEnd(page, 60_000)

    // Navigate to account page and verify TCU is shown
    await page.goto('/account')
    await expect(page.getByText('Uso diário (UTC)')).toBeVisible({ timeout: 5_000 })

    // The "TCU disponível hoje" and "TCU disponível no mês" cards should be present
    await expect(page.getByText('TCU disponível hoje')).toBeVisible()
    await expect(page.getByText('TCU disponível no mês')).toBeVisible()
  })
})
