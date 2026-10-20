import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { apiLogin, apiGetMe, setToken } from '../api/client'

const mockFetch = vi.fn()
global.fetch = mockFetch

describe('apiLogin uses real plan from /auth/me', () => {
  beforeEach(() => {
    mockFetch.mockReset()
    localStorage.clear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('apiLogin returns access_token from /auth/login response', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        access_token: 'tok-123',
        token_type: 'bearer',
        user_id: 'u1',
        email: 'user@example.com',
      }),
    })

    const result = await apiLogin('user@example.com', 'password')
    expect(result.access_token).toBe('tok-123')
    expect(result.email).toBe('user@example.com')
  })

  it('apiGetMe returns plan from server (not hardcoded)', async () => {
    setToken('tok-123')
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        id: 'u1',
        email: 'user@example.com',
        full_name: 'Test User',
        plan: 'pro',
        is_active: true,
      }),
    })

    const me = await apiGetMe()
    expect(me.plan).toBe('pro')
    expect(me.plan).not.toBe('start')
  })

  it('apiGetMe call uses Bearer token from localStorage', async () => {
    setToken('my-token')
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: 'u1', email: 'x@x.com', full_name: '', plan: 'start', is_active: true }),
    })

    await apiGetMe()
    const [, opts] = mockFetch.mock.calls[0] as [string, RequestInit]
    const headers = opts.headers as Record<string, string>
    expect(headers['Authorization']).toBe('Bearer my-token')
  })

  it('apiLogin throws on non-ok response', async () => {
    mockFetch.mockResolvedValueOnce({ ok: false })
    await expect(apiLogin('bad@email.com', 'wrong')).rejects.toThrow('Email ou senha inválidos')
  })
})
