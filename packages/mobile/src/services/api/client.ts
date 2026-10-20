import { API_BASE_URL } from '@/config/env'

import { ApiError, errorFromResponse, fallbackMessage } from './errors'
import { transportFetch, type TransportResponse } from './transport'

export type HttpMethod = 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'

export interface RequestOptions {
  method?: HttpMethod
  body?: unknown
  headers?: Record<string, string>
  signal?: AbortSignal
  /** Time allowed until response headers arrive. */
  timeoutMs?: number
  /** Extra attempts on network failure / 502-504. Defaults to 2 for GET, 0 otherwise. */
  retries?: number
  /** Send the Bearer token. Default true. */
  auth?: boolean
  /** Report a 401 to the session layer so it can verify the session. Default true. */
  notifyUnauthorized?: boolean
}

interface ClientConfig {
  getToken: () => string | null
  onUnauthorized: () => void
  /**
   * Renew the access token after a 401 (one shared attempt for every request
   * that failed together). Resolves true when a new token is ready to retry with.
   */
  refreshAccessToken: () => Promise<boolean>
  baseUrl: string | null
}

let config: ClientConfig = {
  getToken: () => null,
  onUnauthorized: () => undefined,
  refreshAccessToken: async () => false,
  baseUrl: API_BASE_URL,
}

export function configureApiClient(next: Partial<ClientConfig>): void {
  config = { ...config, ...next }
}

export function getClientConfig(): Readonly<ClientConfig> {
  return config
}

export const DEFAULT_TIMEOUT_MS = 20_000
const RETRYABLE_STATUS = new Set([502, 503, 504])

export function buildUrl(path: string): string {
  if (!config.baseUrl) {
    throw new ApiError({ kind: 'config', status: null, message: fallbackMessage('config') })
  }
  return `${config.baseUrl}${path}`
}

export function authHeaders(auth = true): Record<string, string> {
  const token = auth ? config.getToken() : null
  return token ? { Authorization: `Bearer ${token}` } : {}
}

export function handleUnauthorized(error: ApiError, auth: boolean): void {
  if (auth && error.kind === 'unauthorized') config.onUnauthorized()
}

async function readBody(response: TransportResponse): Promise<unknown> {
  const text = await response.text().catch(() => '')
  if (!text) return undefined
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

export interface OpenResponse {
  response: TransportResponse
  /** Detaches the caller's abort signal. Call once the body has been consumed. */
  dispose: () => void
}

/**
 * Sends a request and resolves once headers arrive with a 2xx status. Any other
 * status becomes an ApiError; a 401 on an authenticated call also ends the
 * local session. The body is left unread so streams can be consumed.
 */
export async function openRequest(path: string, options: RequestOptions = {}, handle401 = true): Promise<OpenResponse> {
  const auth = options.auth ?? true
  const url = buildUrl(path)
  const controller = new AbortController()
  const external = options.signal
  const onExternalAbort = () => controller.abort()
  if (external?.aborted) controller.abort()
  external?.addEventListener('abort', onExternalAbort)

  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, options.timeoutMs ?? DEFAULT_TIMEOUT_MS)
  const dispose = () => {
    clearTimeout(timer)
    external?.removeEventListener('abort', onExternalAbort)
  }

  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...authHeaders(auth),
    ...options.headers,
  }
  let body: string | undefined
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.body)
  }

  let response: TransportResponse
  try {
    response = await transportFetch(url, {
      method: options.method ?? 'GET',
      headers,
      body,
      signal: controller.signal,
    })
  } catch (cause) {
    dispose()
    if (external?.aborted) {
      throw new ApiError({ kind: 'aborted', status: null, message: fallbackMessage('aborted'), detail: cause })
    }
    const kind = timedOut ? 'timeout' : 'network'
    throw new ApiError({ kind, status: null, message: fallbackMessage(kind), detail: cause })
  }
  clearTimeout(timer)

  if (!response.ok) {
    const error = errorFromResponse(response.status, await readBody(response))
    dispose()
    if (handle401) handleUnauthorized(error, auth && (options.notifyUnauthorized ?? true))
    throw error
  }
  return { response, dispose }
}

function isRetryable(error: ApiError): boolean {
  if (error.kind === 'network' || error.kind === 'timeout') return true
  return error.status !== null && RETRYABLE_STATUS.has(error.status)
}

export function backoffMs(attempt: number): number {
  return Math.min(4_000, 500 * 2 ** attempt)
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

/**
 * JSON request with centralized auth, timeout, error mapping and bounded retry.
 *
 * A 401 on an authenticated call first renews the access token (the Core's
 * refresh-token rotation) and retries once; only when renewal is impossible is
 * the session layer told, which then decides whether the session really ended.
 */
export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? 'GET'
  const retries = options.retries ?? (method === 'GET' ? 2 : 0)
  const auth = options.auth ?? true
  const notify = auth && (options.notifyUnauthorized ?? true)
  let renewed = false

  for (let attempt = 0; ; attempt++) {
    try {
      const { response, dispose } = await openRequest(path, options, false)
      try {
        if (response.status === 204) return undefined as T
        return (await readBody(response)) as T
      } finally {
        dispose()
      }
    } catch (error) {
      if (error instanceof ApiError && error.kind === 'unauthorized' && auth) {
        if (!renewed && notify && !options.signal?.aborted && (await config.refreshAccessToken())) {
          renewed = true
          attempt--
          continue
        }
        handleUnauthorized(error, notify)
        throw error
      }
      if (!(error instanceof ApiError) || !isRetryable(error) || attempt >= retries || options.signal?.aborted) {
        throw error
      }
      await sleep(backoffMs(attempt))
    }
  }
}
