import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  apiGetMe, apiLogin, apiLogout, bootstrapSession, getStoredToken,
  hasSessionHint, onSessionExpired, refreshSession, setToken,
} from '../api/client'

const mockFetch = vi.fn()
global.fetch = mockFetch

function json(status: number, body: unknown = {}) {
  return { ok: status >= 200 && status < 300, status, json: async () => body }
}

function call(i: number): [string, RequestInit] {
  return mockFetch.mock.calls[i] as [string, RequestInit]
}

describe('persistent web session', () => {
  beforeEach(() => {
    mockFetch.mockReset()
    localStorage.clear()
    setToken(null)
  })

  it('login asks for a web session and never stores the token in localStorage', async () => {
    mockFetch.mockResolvedValueOnce(json(200, { access_token: 'acc-1', token_type: 'bearer', user_id: 'u', email: 'e@x.com' }))
    await apiLogin('e@x.com', 'pw')

    const [url, init] = call(0)
    expect(url).toMatch(/\/auth\/login$/)
    expect(init.credentials).toBe('include')
    expect(JSON.parse(init.body as string).client).toBe('web')
    expect(getStoredToken()).toBe('acc-1')
    expect(JSON.stringify({ ...localStorage })).not.toContain('acc-1')
    expect(hasSessionHint()).toBe(true)
  })

  it('bootstrap restores the session from the refresh cookie after a restart', async () => {
    mockFetch.mockResolvedValueOnce(json(200, { access_token: 'restored' }))
    expect(await bootstrapSession()).toBe(true)

    const [url, init] = call(0)
    expect(url).toMatch(/\/auth\/refresh$/)
    expect(init.credentials).toBe('include')
    expect((init.headers as Record<string, string>)['X-TRINDATRON-Client']).toBe('web')
    expect(getStoredToken()).toBe('restored')
  })

  it('a 401 is recovered by refreshing once and replaying the request', async () => {
    setToken('stale')
    mockFetch
      .mockResolvedValueOnce(json(401))
      .mockResolvedValueOnce(json(200, { access_token: 'fresh' }))
      .mockResolvedValueOnce(json(200, { id: 'u', email: 'e@x.com', plan: 'pro' }))

    const me = await apiGetMe()
    expect(me.plan).toBe('pro')
    expect(mockFetch).toHaveBeenCalledTimes(3)
    expect((call(2)[1].headers as Record<string, string>)['Authorization']).toBe('Bearer fresh')
  })

  it('concurrent 401s share a single refresh', async () => {
    setToken('stale')
    let refreshes = 0
    mockFetch.mockImplementation(async (url: string, init: RequestInit) => {
      if (url.endsWith('/auth/refresh')) {
        refreshes += 1
        await new Promise((r) => setTimeout(r, 5))
        return json(200, { access_token: 'fresh' })
      }
      const auth = (init.headers as Record<string, string>)['Authorization']
      return auth === 'Bearer fresh' ? json(200, { id: 'u', email: 'e', plan: 'pro' }) : json(401)
    })

    await Promise.all([apiGetMe(), apiGetMe(), apiGetMe()])
    expect(refreshes).toBe(1)
  })

  it('a refused refresh ends the session and notifies the app', async () => {
    localStorage.setItem('trindatron_signed_in', '1')
    const expired = vi.fn()
    const off = onSessionExpired(expired)
    mockFetch.mockResolvedValueOnce(json(401))

    expect(await refreshSession()).toBe(false)
    expect(expired).toHaveBeenCalledOnce()
    expect(hasSessionHint()).toBe(false)
    off()
  })

  it('a network failure during refresh does not sign the user out', async () => {
    localStorage.setItem('trindatron_signed_in', '1')
    mockFetch.mockRejectedValueOnce(new TypeError('offline'))
    expect(await refreshSession()).toBe(false)
    expect(hasSessionHint()).toBe(true)
  })

  it('logout revokes server-side and clears local state even offline', async () => {
    setToken('acc')
    localStorage.setItem('trindatron_signed_in', '1')
    mockFetch.mockRejectedValueOnce(new TypeError('offline'))

    await apiLogout()
    expect(call(0)[0]).toMatch(/\/auth\/logout$/)
    expect(call(0)[1].credentials).toBe('include')
    expect(getStoredToken()).toBeNull()
    expect(hasSessionHint()).toBe(false)
  })

  it('auth endpoints are never replayed after a 401', async () => {
    mockFetch.mockResolvedValueOnce(json(401))
    await expect(apiLogin('e@x.com', 'wrong')).rejects.toThrow()
    expect(mockFetch).toHaveBeenCalledTimes(1)
  })
})
