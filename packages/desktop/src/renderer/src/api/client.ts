import type { User, Session, Message, DoneEvent, AgentStep, AgentDoneEvent, StatusEvent } from '../types'

const API_BASE = 'http://localhost:8000'

const TOKEN_KEY = 'trinity_token'

let _token: string | null = localStorage.getItem(TOKEN_KEY)

export function setToken(token: string | null): void {
  _token = token
  if (token) {
    localStorage.setItem(TOKEN_KEY, token)
  } else {
    localStorage.removeItem(TOKEN_KEY)
  }
}

export function getStoredToken(): string | null {
  return _token
}

async function apiFetch(path: string, options: RequestInit = {}): Promise<Response> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (_token) headers['Authorization'] = `Bearer ${_token}`
  Object.assign(headers, options.headers ?? {})
  return fetch(`${API_BASE}${path}`, { ...options, headers })
}

// ── Auth ──────────────────────────────────────────────────────────────────────

export interface AuthResponse {
  access_token: string
  token_type: string
  user_id: string
  email: string
}

export async function apiRegister(
  email: string,
  password: string,
  fullName = '',
): Promise<AuthResponse> {
  const resp = await apiFetch('/auth/register', {
    method: 'POST',
    body: JSON.stringify({ email, password, full_name: fullName }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error((err as { detail?: string }).detail ?? 'Registration failed')
  }
  return resp.json()
}

export async function apiLogin(email: string, password: string): Promise<AuthResponse> {
  const resp = await apiFetch('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })
  if (!resp.ok) throw new Error('Invalid email or password')
  return resp.json()
}

export async function apiGetMe(): Promise<User> {
  const resp = await apiFetch('/auth/me')
  if (!resp.ok) throw new Error('Not authenticated')
  return resp.json()
}

// ── Sessions ──────────────────────────────────────────────────────────────────

export interface SessionListResponse {
  sessions: Session[]
  total: number
}

export interface SessionDetailResponse extends Session {
  messages: Message[]
}

export async function apiListSessions(): Promise<SessionListResponse> {
  const resp = await apiFetch('/sessions')
  if (!resp.ok) throw new Error('Failed to load sessions')
  return resp.json()
}

export async function apiCreateSession(title?: string, model?: string): Promise<Session> {
  const resp = await apiFetch('/sessions', {
    method: 'POST',
    body: JSON.stringify({ title, model }),
  })
  if (!resp.ok) throw new Error('Failed to create session')
  return resp.json()
}

export async function apiGetSession(id: string): Promise<SessionDetailResponse> {
  const resp = await apiFetch(`/sessions/${id}`)
  if (!resp.ok) throw new Error('Session not found')
  return resp.json()
}

export async function apiDeleteSession(id: string): Promise<void> {
  await apiFetch(`/sessions/${id}`, { method: 'DELETE' })
}

// ── Chat SSE ──────────────────────────────────────────────────────────────────

export async function apiStreamChat(
  sessionId: string,
  content: string,
  onText: (chunk: string) => void,
  onDone: (event: DoneEvent) => void,
  onError: (message: string) => void,
): Promise<void> {
  let resp: Response
  try {
    resp = await apiFetch(`/sessions/${sessionId}/chat`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    })
  } catch {
    onError('Cannot connect to TRINDATRON (is it running on port 8000?)')
    return
  }

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    const detail = (err as { detail?: unknown }).detail
    const msg = typeof detail === 'string'
      ? detail
      : (detail && typeof detail === 'object' && typeof (detail as Record<string, unknown>).message === 'string')
        ? (detail as Record<string, unknown>).message as string
        : resp.status === 429 ? 'Limite de uso atingido. Tente novamente mais tarde.' : `HTTP ${resp.status}`
    onError(msg)
    return
  }

  if (!resp.body) {
    onError('No response body')
    return
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const raw = line.slice(6).trim()
        if (raw === '[DONE]') return

        try {
          const event = JSON.parse(raw) as { type: string; content?: string; message?: string } & DoneEvent
          if (event.type === 'text' && event.content) {
            onText(event.content)
          } else if (event.type === 'done') {
            onDone(event as DoneEvent)
          } else if (event.type === 'error') {
            onError(event.message ?? 'Stream error')
            return
          }
        } catch {
          // skip malformed lines
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

// ── Agent SSE ─────────────────────────────────────────────────────────────────

export async function apiStreamAgent(
  sessionId: string,
  content: string,
  onStep: (step: AgentStep) => void,
  onDone: (event: AgentDoneEvent) => void,
  onError: (message: string) => void,
): Promise<void> {
  let resp: Response
  try {
    resp = await apiFetch(`/sessions/${sessionId}/agent`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    })
  } catch {
    onError('Cannot connect to TRINDATRON (is it running on port 8000?)')
    return
  }

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    const detail = (err as { detail?: unknown }).detail
    const msg = typeof detail === 'string'
      ? detail
      : (detail && typeof detail === 'object' && typeof (detail as Record<string, unknown>).message === 'string')
        ? (detail as Record<string, unknown>).message as string
        : resp.status === 429 ? 'Limite de uso atingido. Tente novamente mais tarde.' : `HTTP ${resp.status}`
    onError(msg)
    return
  }

  if (!resp.body) {
    onError('No response body')
    return
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const raw = line.slice(6).trim()
        if (raw === '[DONE]') return

        try {
          const event = JSON.parse(raw) as { type: string; message?: string } & AgentDoneEvent & AgentStep
          if (event.type === 'tool_call' || event.type === 'tool_result') {
            onStep(event as AgentStep)
          } else if (event.type === 'done') {
            onDone(event as AgentDoneEvent)
          } else if (event.type === 'error') {
            onError(event.message ?? 'Agent error')
            return
          }
        } catch {
          // skip malformed lines
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

// ── Unified message SSE ───────────────────────────────────────────────────────

export interface SendMessageHandlers {
  onStatus?: (event: StatusEvent) => void
  onText?: (chunk: string) => void
  onStep?: (step: AgentStep) => void
  onDone?: (event: DoneEvent | AgentDoneEvent) => void
  onError?: (message: string) => void
}

/**
 * Calls the unified /sessions/{id}/message endpoint.
 * The server classifies the message and dispatches to chat or agent pipeline.
 * Handles all SSE event types: status, text, tool_call, tool_result, done, error.
 */
export async function apiSendMessage(
  sessionId: string,
  content: string,
  handlers: SendMessageHandlers,
): Promise<void> {
  let resp: Response
  try {
    resp = await apiFetch(`/sessions/${sessionId}/message`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    })
  } catch {
    handlers.onError?.('Cannot connect to TRINDATRON (is it running on port 8000?)')
    return
  }

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    const detail = (err as { detail?: unknown }).detail
    const msg =
      typeof detail === 'string'
        ? detail
        : detail &&
            typeof detail === 'object' &&
            typeof (detail as Record<string, unknown>).message === 'string'
          ? ((detail as Record<string, unknown>).message as string)
          : resp.status === 429
            ? 'Limite de uso atingido. Tente novamente mais tarde.'
            : `HTTP ${resp.status}`
    handlers.onError?.(msg)
    return
  }

  if (!resp.body) {
    handlers.onError?.('No response body')
    return
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const raw = line.slice(6).trim()
        if (raw === '[DONE]') return

        try {
          const event = JSON.parse(raw) as {
            type: string
            key?: string
            message?: string
            content?: string
          } & AgentStep &
            AgentDoneEvent &
            DoneEvent

          switch (event.type) {
            case 'status':
              handlers.onStatus?.({
                type: 'status',
                key: event.key ?? event.message ?? '',
                message: event.message ?? '',
              })
              break
            case 'text':
              if (event.content) handlers.onText?.(event.content)
              break
            case 'tool_call':
            case 'tool_result':
              handlers.onStep?.(event as AgentStep)
              break
            case 'done':
              handlers.onDone?.(event as DoneEvent | AgentDoneEvent)
              break
            case 'error':
              handlers.onError?.(event.message ?? 'Stream error')
              return
          }
        } catch {
          // skip malformed lines
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}
