import { test, expect } from '@playwright/test'
import { registerAndLogin, TEST_PASSWORD } from './helpers'

test.describe('Auth flow', () => {
  test('root serves the public landing page, not a redirect', async ({ page }) => {
    // `/` is the commercial home now — it must render for signed-out
    // visitors instead of bouncing them into the app's auth screen.
    await page.goto('/')
    await expect(page).toHaveURL(/\/$/)
    await expect(page.getByRole('heading', { level: 1 })).toContainText('Você descreve o trabalho')
  })

  test('landing CTA opens the register tab', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('link', { name: /Testar grátis/ }).first().click()
    await expect(page).toHaveURL(/\/auth\?signup=1/, { timeout: 10_000 })
    // ?signup=1 must preselect "Criar conta" so a visitor who came to sign
    // up does not land on the login form and have to find the switch.
    await expect(page.getByPlaceholder('Seu nome')).toBeVisible({ timeout: 10_000 })
  })

  test('register → redirect to /home with real plan from server', async ({ page }) => {
    // Each test gets its own unique email to avoid registration conflicts
    const email = `e2e-reg-${Date.now()}@example.com`
    await registerAndLogin(page, email, TEST_PASSWORD)
    await expect(page).toHaveURL(/\/home/)

    // Sidebar must show email (real data from /auth/me, never hardcoded)
    const sidebar = page.locator('aside')
    await expect(sidebar).toContainText(email, { timeout: 5_000 })

    // Plan badge should be visible (any non-empty plan label)
    // The plan comes from the server via apiGetMe(), not hardcoded 'start'
    const planBadge = sidebar.locator('p').filter({ hasText: /FREE|START|PRO|MAX|TEAM/i })
    await expect(planBadge).toBeVisible()
  })

  test('logout clears session and redirects to /auth', async ({ page }) => {
    const email = `e2e-logout-${Date.now()}@example.com`
    await registerAndLogin(page, email, TEST_PASSWORD)
    await expect(page).toHaveURL(/\/home/)

    // Click logout button in sidebar footer
    await page.locator('aside').getByTitle('Sair').click()
    await expect(page).toHaveURL(/\/auth/, { timeout: 5_000 })

    // Navigating to /home should redirect back to /auth (token cleared)
    await page.goto('/home')
    await expect(page).toHaveURL(/\/auth/, { timeout: 5_000 })
  })

  test('login with existing account works', async ({ page }) => {
    const email = `e2e-login-${Date.now()}@example.com`
    // Register first
    await registerAndLogin(page, email, TEST_PASSWORD)
    // Logout
    await page.locator('aside').getByTitle('Sair').click()
    await expect(page).toHaveURL(/\/auth/)

    // Login with the same credentials
    // The login tab is active after logout redirect; use .last() to click the
    // submit button (not the tab button that also has the text "Entrar")
    await page.getByPlaceholder('voce@email.com').fill(email)
    await page.getByPlaceholder('••••••••').fill(TEST_PASSWORD)
    await page.getByRole('button', { name: 'Entrar' }).last().click()
    await expect(page).toHaveURL(/\/home/, { timeout: 15_000 })
  })
})
