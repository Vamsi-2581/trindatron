import { test, expect } from '@playwright/test'

/**
 * The public Home. Needs the backend for the plan catalogue and the sign-up
 * gate; the gate is only opened here, never submitted, so no account is made.
 */

test.describe('home', () => {
  test('first screen: promise, field and language switch', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('heading', { level: 1 })).toContainText('You speak.')
    await expect(page.locator('#hero-ask')).toBeVisible()

    await page.getByRole('group', { name: 'Language' }).getByRole('button', { name: 'PT' }).click()
    await expect(page.getByRole('heading', { level: 1 })).toContainText('Você fala.')
    await expect(page.locator('html')).toHaveAttribute('lang', 'pt-BR')

    await page.getByRole('group', { name: 'Idioma' }).getByRole('button', { name: 'ES' }).click()
    await expect(page.getByRole('heading', { level: 1 })).toContainText('Tú hablas.')
  })

  test('asking from the hero reaches the real conversation, behind sign-up', async ({ page }) => {
    await page.goto('/')
    await page.fill('#hero-ask', 'Plan my week around three deadlines')
    await page.locator('#hero-ask').press('Enter')
    await expect(page.getByTestId('auth-gate')).toBeVisible({ timeout: 15_000 })
    await expect(page.locator('#try')).toContainText('Plan my week around three deadlines')
  })

  test('plans are priced by the server catalogue', async ({ page }) => {
    await page.goto('/')
    const plans = page.locator('#pricing')
    await plans.scrollIntoViewIfNeeded()
    await expect(plans).toContainText(/\d+[.,]\d{2}/, { timeout: 15_000 })
  })

  test('the real-session film is present and nothing is labelled as coming soon', async ({ page }) => {
    await page.goto('/')
    await expect(page.locator('#film video source')).toHaveAttribute('src', /\/home\/film\/trindatron-demo-en-(land|port)\.mp4$/)
    await expect(page.locator('#film')).toContainText('Real session')
    await expect(page.getByTestId('replay-web')).toContainText('Real session')
    await expect(page.locator('body')).not.toContainText(/coming soon|in development|arriving/i)
  })

  test('the studio shows the ten capabilities, the most visual first', async ({ page }) => {
    await page.goto('/')
    const order = await page.locator('#studio article').evaluateAll((rows) => rows.map((r) => r.id))
    expect(order).toEqual(['video', 'images', 'editing', 'motion', 'vision', 'avatar', 'slides', 'docs', 'computer', 'code'].map((k) => `studio-${k}`))
    await expect(page.getByTestId('make-video').locator('video source')).toHaveAttribute('src', '/home/media/video-en.mp4')
    await expect(page.getByTestId('make-image').locator('img[alt*="TRINDATRON created"]')).toHaveAttribute('src', '/home/media/image-en.jpg')
    await expect(page.getByTestId('edit-demo').locator('video source')).toHaveAttribute('src', '/home/media/edit/edit-en.mp4')
    await expect(page.getByTestId('avatar-demo').locator('video source')).toHaveAttribute('src', '/home/media/avatar-en.mp4')
    for (const cap of ['slides', 'docs', 'computer', 'code']) {
      await expect(page.getByTestId(`capfilm-${cap}`).locator('video source')).toHaveAttribute('src', `/home/caps/cap-${cap}-en.mp4`)
    }
    const studioTop = await page.locator('#studio').evaluate((el) => el.getBoundingClientRect().top + window.scrollY)
    const everydayTop = await page.locator('#today').evaluate((el) => el.getBoundingClientRect().top + window.scrollY)
    expect(studioTop).toBeLessThan(everydayTop)
  })

  test('scrolling is never held: no pinned chapter, no box that traps the wheel, full travel per turn', async ({ page }) => {
    test.setTimeout(240_000) // the page is long, and the software WebGL used here is slow
    await page.goto('/')
    await page.waitForTimeout(1500)
    expect(await page.locator('.pin-spacer').count()).toBe(0)
    await page.mouse.move(720, 450)
    const bottom = () => page.evaluate(() => document.documentElement.scrollHeight - window.innerHeight)
    for (let i = 0; i < 200; i++) {
      const before = await page.evaluate(() => window.scrollY)
      const height = await bottom()
      if (before >= height - 2) break
      // Anything under the pointer that would scroll on its own instead of the page (the chat is the one window allowed to).
      const trap = await page.evaluate(() => {
        for (let n = document.elementFromPoint(720, 450); n && n !== document.body; n = n.parentElement) {
          const cs = getComputedStyle(n)
          if (/(auto|scroll)/.test(cs.overflowY) && n.scrollHeight > n.clientHeight + 1 && !n.closest('#try')) return n.className.toString()
        }
        return ''
      })
      expect(trap, `a box under the pointer scrolls instead of the page at ${before}px`).toBe('')
      // The software renderer used here can drop a synthetic wheel event while it is busy; one retry tells
      // a dropped event from a page that holds the scroll (that one would still not move).
      const target = Math.min(before + 590, height - 2)
      const moved = async () => (await page.evaluate(() => window.scrollY)) >= target
      await page.mouse.wheel(0, 600)
      await expect.poll(moved, { timeout: 4000 }).toBe(true).catch(async () => {
        await page.mouse.wheel(0, 600)
        await expect.poll(moved, { timeout: 4000 }).toBe(true)
      })
    }
    expect(await page.evaluate(() => window.scrollY)).toBeGreaterThanOrEqual((await bottom()) - 2)
  })

  test('the official symbol is in the header on a phone', async ({ browser }) => {
    const context = await browser.newContext({ viewport: { width: 360, height: 800 }, isMobile: true, hasTouch: true })
    const page = await context.newPage()
    await page.goto('/')
    const symbol = page.locator('header a[href="#top"] img')
    await expect(symbol).toBeVisible()
    await expect(symbol).toHaveAttribute('src', /trindatron-lockup-vertical/)
    await context.close()
  })

  test('no horizontal overflow on a phone', async ({ browser }) => {
    const context = await browser.newContext({ viewport: { width: 360, height: 800 }, isMobile: true, hasTouch: true })
    const page = await context.newPage()
    await page.goto('/')
    const wide = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1)
    expect(wide).toBe(false)
    await context.close()
  })
})
