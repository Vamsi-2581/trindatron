import { apiRequest, configureApiClient } from '../client'
import { ApiError } from '../errors'
import { transportFetch, type TransportInit } from '../transport'
import { jsonResponse } from '@/test/transport'

jest.mock('../transport', () => ({ transportFetch: jest.fn() }))

const mockFetch = transportFetch as jest.MockedFunction<typeof transportFetch>
const onUnauthorized = jest.fn()
let token: string | null = 'user-token'

beforeEach(() => {
  mockFetch.mockReset()
  onUnauthorized.mockReset()
  token = 'user-token'
  configureApiClient({ baseUrl: 'https://core.test', getToken: () => token, onUnauthorized })
})

describe('apiRequest', () => {
  it('sends the Bearer token and a JSON body, and parses the JSON reply', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(201, { id: 'p1' }))
    const result = await apiRequest<{ id: string }>('/projects', { method: 'POST', body: { name: 'X' } })

    expect(result).toEqual({ id: 'p1' })
    const [url, init] = mockFetch.mock.calls[0] as [string, TransportInit]
    expect(url).toBe('https://core.test/projects')
    expect(init.method).toBe('POST')
    expect(init.headers.Authorization).toBe('Bearer user-token')
    expect(init.headers['Content-Type']).toBe('application/json')
    expect(init.body).toBe('{"name":"X"}')
  })

  it('omits the token on unauthenticated calls', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(200, {}))
    await apiRequest('/auth/login', { method: 'POST', body: {}, auth: false })
    expect((mockFetch.mock.calls[0]![1] as TransportInit).headers.Authorization).toBeUndefined()
  })

  it('returns undefined for 204', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(204))
    await expect(apiRequest('/sessions/s1', { method: 'DELETE' })).resolves.toBeUndefined()
  })

  it('ends the session on 401 from an authenticated call', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(401, { detail: 'Invalid or expired token' }))
    await expect(apiRequest('/auth/me')).rejects.toMatchObject({ kind: 'unauthorized' })
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('does not end the session for a 401 on an unauthenticated call', async () => {
    mockFetch.mockResolvedValueOnce(jsonResponse(401, { detail: 'Incorrect email or password' }))
    await expect(apiRequest('/auth/login', { method: 'POST', auth: false })).rejects.toBeInstanceOf(ApiError)
    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('retries a GET after a network failure', async () => {
    mockFetch.mockRejectedValueOnce(new TypeError('Network request failed')).mockResolvedValueOnce(jsonResponse(200, { ok: true }))
    await expect(apiRequest('/projects')).resolves.toEqual({ ok: true })
    expect(mockFetch).toHaveBeenCalledTimes(2)
  })

  it('retries a GET on 503 but gives up after the retry budget', async () => {
    mockFetch.mockResolvedValue(jsonResponse(503, {}))
    await expect(apiRequest('/projects', { retries: 1 })).rejects.toMatchObject({ kind: 'dependency_unavailable' })
    expect(mockFetch).toHaveBeenCalledTimes(2)
  })

  it('never retries a POST by default', async () => {
    mockFetch.mockRejectedValue(new TypeError('Network request failed'))
    await expect(apiRequest('/projects', { method: 'POST', body: {} })).rejects.toMatchObject({ kind: 'network' })
    expect(mockFetch).toHaveBeenCalledTimes(1)
  })

  it('reports a timeout when headers never arrive', async () => {
    mockFetch.mockImplementation(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => reject(new Error('aborted')))
        }),
    )
    await expect(apiRequest('/projects', { timeoutMs: 20, retries: 0 })).rejects.toMatchObject({ kind: 'timeout' })
  })

  it('reports aborted when the caller cancels', async () => {
    const controller = new AbortController()
    mockFetch.mockImplementation(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => reject(new Error('aborted')))
        }),
    )
    const pending = apiRequest('/projects', { signal: controller.signal })
    controller.abort()
    await expect(pending).rejects.toMatchObject({ kind: 'aborted' })
    expect(mockFetch).toHaveBeenCalledTimes(1)
  })

  it('refuses to run without a configured Core URL', async () => {
    configureApiClient({ baseUrl: null })
    await expect(apiRequest('/projects')).rejects.toMatchObject({ kind: 'config' })
    expect(mockFetch).not.toHaveBeenCalled()
    configureApiClient({ baseUrl: 'https://core.test' })
  })
})

describe('an expired access token', () => {
  afterEach(() => configureApiClient({ refreshAccessToken: async () => false }))

  it('is renewed and the request is retried once with the new token', async () => {
    const refreshAccessToken = jest.fn(async () => {
      token = 'renewed-token'
      return true
    })
    configureApiClient({ refreshAccessToken })
    mockFetch.mockResolvedValueOnce(jsonResponse(401, { detail: 'Invalid or expired token' })).mockResolvedValueOnce(jsonResponse(200, { ok: true }))

    await expect(apiRequest('/projects', { method: 'POST', body: {} })).resolves.toEqual({ ok: true })
    expect(refreshAccessToken).toHaveBeenCalledTimes(1)
    expect((mockFetch.mock.calls[1]![1] as TransportInit).headers.Authorization).toBe('Bearer renewed-token')
    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('tells the session layer when renewal is impossible, and never loops', async () => {
    const refreshAccessToken = jest.fn(async () => false)
    configureApiClient({ refreshAccessToken })
    mockFetch.mockResolvedValue(jsonResponse(401, { detail: 'Invalid or expired token' }))

    await expect(apiRequest('/projects')).rejects.toMatchObject({ kind: 'unauthorized' })
    expect(refreshAccessToken).toHaveBeenCalledTimes(1)
    expect(mockFetch).toHaveBeenCalledTimes(1)
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('a second 401 after a successful renewal is reported, not retried again', async () => {
    configureApiClient({ refreshAccessToken: jest.fn(async () => true) })
    mockFetch.mockResolvedValue(jsonResponse(401, { detail: 'Invalid or expired token' }))
    await expect(apiRequest('/projects')).rejects.toMatchObject({ kind: 'unauthorized' })
    expect(mockFetch).toHaveBeenCalledTimes(2)
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('does not renew for a sign-in attempt', async () => {
    const refreshAccessToken = jest.fn(async () => true)
    configureApiClient({ refreshAccessToken })
    mockFetch.mockResolvedValueOnce(jsonResponse(401, { detail: 'Incorrect email or password' }))
    await expect(apiRequest('/auth/login', { method: 'POST', auth: false })).rejects.toBeInstanceOf(ApiError)
    expect(refreshAccessToken).not.toHaveBeenCalled()
  })
})
