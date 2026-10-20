import { describe, it, expect, vi, beforeEach } from 'vitest'
import { apiListAgents, apiCreateSession, setToken } from '../api/client'

const mockFetch = vi.fn()
global.fetch = mockFetch

describe('agent-selector — apiListAgents', () => {
  beforeEach(() => {
    mockFetch.mockReset()
    localStorage.clear()
    setToken('test-token')
  })

  it('renders agent list: returns agents from API', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        agents: [
          { id: 'a1', name: 'Pesquisador', avatar_emoji: '🔍', is_archived: false },
          { id: 'a2', name: 'Escritor', avatar_emoji: '✍️', is_archived: false },
        ],
        total: 2,
      }),
    })
    const result = await apiListAgents()
    expect(result.agents).toHaveLength(2)
    expect(result.agents[0].id).toBe('a1')
    expect(result.agents[1].name).toBe('Escritor')
  })

  it('lista vazia: empty agent list returns zero items', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ agents: [], total: 0 }),
    })
    const result = await apiListAgents()
    expect(result.agents).toHaveLength(0)
  })

  it('erro no carregamento: apiListAgents failure throws (UI catches it silently)', async () => {
    mockFetch.mockResolvedValueOnce({ ok: false })
    await expect(apiListAgents()).rejects.toThrow()
  })
})

describe('agent-selector — apiCreateSession com agente', () => {
  beforeEach(() => {
    mockFetch.mockReset()
    localStorage.clear()
    setToken('test-token')
  })

  it('seleção de agente: sends agent_definition_id in body when agent is selected', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: 's1', title: null, created_at: '', updated_at: '' }),
    })
    await apiCreateSession(undefined, undefined, 'agent-abc')
    const [, opts] = mockFetch.mock.calls[0] as [unknown, { body: string }]
    const body = JSON.parse(opts.body)
    expect(body.agent_definition_id).toBe('agent-abc')
  })

  it('estado sem agente: sends null agent_definition_id when no agent selected', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: 's2', title: null, created_at: '', updated_at: '' }),
    })
    await apiCreateSession()
    const [, opts] = mockFetch.mock.calls[0] as [unknown, { body: string }]
    const body = JSON.parse(opts.body)
    expect(body.agent_definition_id).toBeNull()
  })

  it('criação normal sem agente: session created successfully without agentDefinitionId', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: 's3', title: null, created_at: '', updated_at: '' }),
    })
    const session = await apiCreateSession()
    expect(session.id).toBe('s3')
  })

  it('lista vazia não bloqueia chat: empty agent list still allows session creation', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ agents: [], total: 0 }),
    })
    const list = await apiListAgents()
    expect(list.agents).toHaveLength(0)

    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: 's4', title: null, created_at: '', updated_at: '' }),
    })
    const session = await apiCreateSession()
    expect(session.id).toBe('s4')
  })

  it('erro no carregamento não bloqueia chat: agent load failure does not prevent session creation', async () => {
    mockFetch.mockResolvedValueOnce({ ok: false })
    await expect(apiListAgents()).rejects.toThrow()

    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ id: 's5', title: null, created_at: '', updated_at: '' }),
    })
    const session = await apiCreateSession()
    expect(session.id).toBe('s5')
  })
})
