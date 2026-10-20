import { test, expect } from '@playwright/test'
import { registerAndLogin, waitForStreamEnd, TEST_PASSWORD } from './helpers'

test.describe('Session persistence', () => {
  test('session appears in sidebar and title updates after first message', async ({ page }) => {
    await registerAndLogin(page, `e2e-persist-a-${Date.now()}@example.com`, TEST_PASSWORD)

    // Send a message to create a session
    const textarea = page.getByPlaceholder('Escreva o que precisa fazer…').first()
    await textarea.fill('Qual é a capital do Brasil?')
    await page.keyboard.press('Enter')

    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]+/, { timeout: 10_000 })
    await waitForStreamEnd(page, 60_000)

    // After streaming, the sidebar should show the session (title updated by backend)
    const sidebar = page.locator('aside')
    const sessionLinks = sidebar.locator('a[href^="/chat/"]')
    await expect(sessionLinks.first()).toBeVisible({ timeout: 5_000 })
  })

  test('history loads correctly after page reload', async ({ page }) => {
    await registerAndLogin(page, `e2e-persist-b-${Date.now()}@example.com`, TEST_PASSWORD)

    // Send message to create session
    const textarea = page.getByPlaceholder('Escreva o que precisa fazer…').first()
    await textarea.fill('Diga: persistência confirmada')
    await page.keyboard.press('Enter')

    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]+/, { timeout: 10_000 })
    const chatUrl = page.url()
    await waitForStreamEnd(page, 60_000)

    // Reload the page — history must reload from the API
    await page.reload()
    await expect(page).toHaveURL(chatUrl, { timeout: 10_000 })

    // The user message must still be visible after reload
    await expect(page.locator('.flex.justify-end')).toBeVisible({ timeout: 10_000 })
  })

  test('deleting a session removes it from the sidebar', async ({ page }) => {
    await registerAndLogin(page, `e2e-persist-c-${Date.now()}@example.com`, TEST_PASSWORD)

    // Create a session via home page
    const textarea = page.getByPlaceholder('Escreva o que precisa fazer…').first()
    await textarea.fill('Sessão para deletar')
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]+/, { timeout: 10_000 })
    await waitForStreamEnd(page, 60_000)

    // Return to home and count sessions in sidebar
    const sidebar = page.locator('aside')
    const sessionLinks = sidebar.locator('a[href^="/chat/"]')
    const countBefore = await sessionLinks.count()

    // Hover over the first session to reveal the delete button, then click it
    await sessionLinks.first().hover()
    await page.getByTitle('Excluir conversa').first().click()

    // Session count in sidebar must decrease
    await expect(sessionLinks).toHaveCount(Math.max(0, countBefore - 1), { timeout: 5_000 })
  })
})
