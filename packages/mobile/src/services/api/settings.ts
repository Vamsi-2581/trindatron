import { apiRequest, buildUrl, authHeaders } from './client'

/**
 * Everything a person can change or take with them: how TRINDATRON answers,
 * where they are signed in, what it remembers, and a copy of their own data.
 *
 * Only what the Core actually honours appears here. There is deliberately no
 * colour theme and no spoken-input language — the Core refuses both — and no
 * account deletion, which does not exist yet. A control for any of them would
 * be a switch wired to nothing.
 */
export type LanguagePreference = 'auto' | 'pt' | 'en' | 'es'
export type ResponseStyle = 'balanced' | 'concise' | 'detailed'

export interface Preferences {
  language: LanguagePreference
  response_style: ResponseStyle
  /** Clients where a keyboard has Enter; on a phone the send button is always the action. */
  send_on_enter: boolean
  /** Read each finished answer aloud, when voice output is available. */
  voice_auto_play: boolean
  memory_enabled: boolean
  disabled_plugins: string[]
}

export type PreferencesUpdate = Partial<Omit<Preferences, 'disabled_plugins'>> & { disabled_plugins?: string[] }

export function getPreferences(signal?: AbortSignal): Promise<Preferences> {
  return apiRequest<Preferences>('/users/me/preferences', { signal })
}

export function updatePreferences(changes: PreferencesUpdate): Promise<Preferences> {
  return apiRequest<Preferences>('/users/me/preferences', { method: 'PATCH', body: changes })
}

/** One sign-in, as the Core describes it. Never a token, never anything replayable. */
export interface DeviceSession {
  id: string
  client: string
  user_agent: string | null
  started_at: string
  last_used_at: string
  expires_at: string
  current: boolean
}

export function listDeviceSessions(signal?: AbortSignal): Promise<{ sessions: DeviceSession[] }> {
  return apiRequest<{ sessions: DeviceSession[] }>('/auth/sessions', { signal })
}

export function revokeDeviceSession(sessionId: string): Promise<void> {
  return apiRequest<void>(`/auth/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
}

/** Ends every session, including this one: the caller signs out locally afterwards. */
export function signOutEverywhere(): Promise<void> {
  return apiRequest<void>('/auth/sessions/revoke-all', { method: 'POST' })
}

export interface Memory {
  id: string
  content: string
  created_at: string
}

export function listMemories(signal?: AbortSignal): Promise<Memory[]> {
  return apiRequest<Memory[]>('/users/me/memories', { signal })
}

export function deleteMemory(memoryId: string): Promise<void> {
  return apiRequest<void>(`/users/me/memories/${encodeURIComponent(memoryId)}`, { method: 'DELETE' })
}

export function clearMemories(): Promise<{ deleted: number }> {
  return apiRequest<{ deleted: number }>('/users/me/memories', { method: 'DELETE' })
}

/**
 * The person's own data as one JSON document. Fetched as text rather than
 * through apiRequest so it can be written to a file and shared: what the Core
 * sends is what is saved, with nothing re-encoded on the way.
 */
export async function exportMyData(signal?: AbortSignal): Promise<string> {
  const response = await fetch(buildUrl('/users/me/export'), { headers: authHeaders(), signal })
  if (!response.ok) {
    const { errorFromResponse } = await import('./errors')
    let detail: unknown = {}
    try {
      detail = await response.json()
    } catch {
      // A non-JSON failure still has its status.
    }
    throw errorFromResponse(response.status, detail)
  }
  return response.text()
}
