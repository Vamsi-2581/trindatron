import { apiRequest } from '../client'
import {
  clearMemories,
  deleteMemory,
  getPreferences,
  listDeviceSessions,
  listMemories,
  revokeDeviceSession,
  signOutEverywhere,
  updatePreferences,
} from '../settings'

jest.mock('../client', () => ({
  apiRequest: jest.fn(),
  authHeaders: jest.fn(() => ({ Authorization: 'Bearer t' })),
  buildUrl: jest.fn((path: string) => `https://core.example.com${path}`),
}))

const request = apiRequest as jest.Mock

describe('settings API', () => {
  beforeEach(() => request.mockReset().mockResolvedValue({}))

  it('reads and changes only the preferences the Core honours', async () => {
    await getPreferences()
    expect(request).toHaveBeenCalledWith('/users/me/preferences', { signal: undefined })

    await updatePreferences({ response_style: 'concise', voice_auto_play: true })
    expect(request).toHaveBeenLastCalledWith('/users/me/preferences', {
      method: 'PATCH',
      body: { response_style: 'concise', voice_auto_play: true },
    })
  })

  it('lists devices and ends one session or every session', async () => {
    await listDeviceSessions()
    expect(request).toHaveBeenCalledWith('/auth/sessions', { signal: undefined })

    await revokeDeviceSession('fam/1')
    expect(request).toHaveBeenLastCalledWith('/auth/sessions/fam%2F1', { method: 'DELETE' })

    await signOutEverywhere()
    expect(request).toHaveBeenLastCalledWith('/auth/sessions/revoke-all', { method: 'POST' })
  })

  it('reads, deletes one and clears every memory', async () => {
    await listMemories()
    expect(request).toHaveBeenCalledWith('/users/me/memories', { signal: undefined })

    await deleteMemory('m 1')
    expect(request).toHaveBeenLastCalledWith('/users/me/memories/m%201', { method: 'DELETE' })

    await clearMemories()
    expect(request).toHaveBeenLastCalledWith('/users/me/memories', { method: 'DELETE' })
  })

  it('sends exactly the keys it was given, and invents none', async () => {
    // The Core refuses unknown keys outright (extra="forbid"), so a client that helpfully added
    // a theme or a spoken-input language would break the whole save, not just that setting.
    await updatePreferences({ memory_enabled: false })
    expect(request).toHaveBeenLastCalledWith('/users/me/preferences', {
      method: 'PATCH',
      body: { memory_enabled: false },
    })
  })

  it('lets a refusal from the Core through instead of interpreting it', async () => {
    request.mockRejectedValueOnce(new Error('403'))
    await expect(updatePreferences({ language: 'pt' })).rejects.toThrow('403')
  })
})
