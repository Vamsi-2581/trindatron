import { test, expect } from '@playwright/test'
import { registerAndLogin, waitForStreamEnd, TEST_PASSWORD } from './helpers'

// Each test generates its own unique email — sharing a module-level email causes
// registration conflicts across tests (second+ registration returns 409).
test.describe('Chat streaming', () => {
  test('typing and sending a message streams a response', async ({ page }) => {
    await registerAndLogin(page, `e2e-chat-a-${Date.now()}@example.com`, TEST_PASSWORD)

    // Send a short, deterministic prompt that any model will answer
    const textarea = page.getByPlaceholder('Escreva o que precisa fazer…').first()
    await textarea.fill('Responda apenas: OK')
    await page.keyboard.press('Enter')

    // Wait for navigation to the chat page
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]+/, { timeout: 10_000 })

    // Wait for the user message text to appear — confirms the pending message was dispatched
    // and the chat area is actually rendering messages (not the empty state)
    await expect(page.getByText('Responda apenas: OK')).toBeVisible({ timeout: 15_000 })

    // Wait for streaming to complete
    await waitForStreamEnd(page, 60_000)

    // After completion, textarea is cleared
    await expect(page.getByPlaceholder('Escreva o que precisa fazer…')).toHaveValue('')
  })

  test('send button is disabled while streaming', async ({ page }) => {
    await registerAndLogin(page, `e2e-chat-b-${Date.now()}@example.com`, TEST_PASSWORD)

    const textarea = page.getByPlaceholder('Escreva o que precisa fazer…')
    await textarea.fill('Diga apenas: olá')
    await page.getByRole('button', { name: 'Enviar' }).click()

    // Immediately after click, send button should be disabled (streaming)
    const sendBtn = page.getByRole('button', { name: 'Enviar' })
    await expect(sendBtn).toBeDisabled({ timeout: 5_000 })

    // After stream ends, button is no longer disabled
    await waitForStreamEnd(page, 60_000)
  })

  test('new conversation from home page navigates to chat and sends pending message', async ({ page }) => {
    await registerAndLogin(page, `e2e-chat-d-${Date.now()}@example.com`, TEST_PASSWORD)

    const textarea = page.getByPlaceholder('Escreva o que precisa fazer…').first()
    await textarea.fill('Teste de sessão pendente')

    // Submit via home page composer
    await page.keyboard.press('Enter')

    // Should navigate to /chat/<sessionId>
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]+/, { timeout: 10_000 })

    // The pending message should have been sent automatically
    await waitForStreamEnd(page, 60_000)
  })
})
