import type { SessionDetail } from '@/services/api/types'

import { chatReducer, initialChatState, type ChatState } from '../chatReducer'

const detail: SessionDetail = {
  id: 's1',
  title: 'Planejamento',
  created_at: '2026-09-13T10:00:00',
  updated_at: '2026-09-13T10:00:00',
  messages: [
    { id: 'm0', session_id: 's1', role: 'system', content: 'hidden', created_at: '2026-09-13T10:00:00' },
    { id: 'm1', session_id: 's1', role: 'user', content: 'oi', created_at: '2026-09-13T10:00:01' },
    { id: 'm2', session_id: 's1', role: 'assistant', content: 'olá', created_at: '2026-09-13T10:00:02' },
  ],
}

function started(): ChatState {
  const loaded = chatReducer(initialChatState, { type: 'loaded', detail })
  return chatReducer(loaded, { type: 'turnStarted', content: 'nova pergunta', turnId: 't1', now: '2026-09-13T10:01:00Z' })
}

describe('chatReducer', () => {
  it('loads history without system messages', () => {
    const state = chatReducer(initialChatState, { type: 'loaded', detail })
    expect(state.phase).toBe('ready')
    expect(state.title).toBe('Planejamento')
    expect(state.messages.map((message) => message.id)).toEqual(['m1', 'm2'])
  })

  it('runs a successful turn and adopts the server ids', () => {
    let state = started()
    expect(state.streaming).toBe(true)
    state = chatReducer(state, { type: 'activity', message: 'Processando' })
    state = chatReducer(state, { type: 'textAppended', chunk: 'Res' })
    state = chatReducer(state, { type: 'textAppended', chunk: 'posta' })
    expect(state.activity).toBe('Processando')

    state = chatReducer(state, { type: 'turnDone', event: { type: 'done', session_id: 's1', message_id: 'm4', user_message_id: 'm3' } })
    expect(state.streaming).toBe(false)
    expect(state.activeTurn).toBeNull()
    expect(state.activity).toBeNull()
    expect(state.messages.slice(-2)).toEqual([
      expect.objectContaining({ id: 'm3', role: 'user', content: 'nova pergunta' }),
      expect.objectContaining({ id: 'm4', role: 'assistant', content: 'Resposta', state: 'final' }),
    ])
  })

  it('removes the empty placeholder and records a retryable error when a turn fails', () => {
    const state = chatReducer(started(), {
      type: 'turnFailed',
      error: { message: 'An internal error occurred.', code: null, content: 'nova pergunta', retryable: true },
    })
    expect(state.streaming).toBe(false)
    expect(state.messages.map((message) => message.role)).toEqual(['user', 'assistant', 'user'])
    expect(state.turnError).toMatchObject({ content: 'nova pergunta', retryable: true })
  })

  it('keeps partial text marked as failed when the stream breaks mid-answer', () => {
    let state = chatReducer(started(), { type: 'textAppended', chunk: 'meia resposta' })
    state = chatReducer(state, { type: 'turnFailed', error: { message: 'x', code: 'interrupted', content: 'nova pergunta', retryable: true } })
    expect(state.messages[state.messages.length - 1]).toMatchObject({ content: 'meia resposta', state: 'failed' })
  })

  it('marks a stopped answer and drops an empty one', () => {
    const partial = chatReducer(chatReducer(started(), { type: 'textAppended', chunk: 'algo' }), { type: 'turnStopped' })
    expect(partial.messages[partial.messages.length - 1]).toMatchObject({ state: 'stopped' })

    const empty = chatReducer(started(), { type: 'turnStopped' })
    expect(empty.messages[empty.messages.length - 1]).toMatchObject({ role: 'user' })
  })

  it('tracks agent steps and approvals only while streaming', () => {
    let state = chatReducer(started(), { type: 'agentStep', step: { type: 'tool_call', tool: 'search' } })
    state = chatReducer(state, { type: 'approvalRequired', event: { action_id: 'a', action: 'write_file', args: {}, description: 'd' } })
    expect(state.agentSteps).toHaveLength(1)
    expect(state.approval?.action_id).toBe('a')

    const idle = chatReducer(initialChatState, { type: 'activity', message: 'late' })
    expect(idle.activity).toBeNull()
  })

  it('keeps the conversation visible when a reload fails', () => {
    const loaded = chatReducer(initialChatState, { type: 'loaded', detail })
    expect(chatReducer(loaded, { type: 'loadFailed', message: 'sem rede' })).toMatchObject({ phase: 'ready', notice: 'sem rede' })
    expect(chatReducer(initialChatState, { type: 'loadFailed', message: 'sem rede' })).toMatchObject({ phase: 'error', loadError: 'sem rede' })
  })
})

describe('an answer cut at the plan limit', () => {
  it('is marked when the turn ends at the limit, and only the answer is', () => {
    let state = chatReducer(started(), { type: 'textAppended', chunk: '| 22:00 – 22' })
    state = chatReducer(state, {
      type: 'turnDone',
      event: { type: 'done', session_id: 's1', message_id: 'm4', user_message_id: 'm3', truncated: true },
    })
    const [question, answer] = state.messages.slice(-2)
    expect(answer).toMatchObject({ id: 'm4', truncated: true })
    expect(question?.truncated).toBeUndefined()
  })

  it('is not marked when the answer finished', () => {
    let state = chatReducer(started(), { type: 'textAppended', chunk: 'Pronto.' })
    state = chatReducer(state, { type: 'turnDone', event: { type: 'done', session_id: 's1', message_id: 'm4', truncated: false } })
    expect(state.messages.at(-1)?.truncated).toBeUndefined()
  })

  it('stays marked when the conversation is opened again', () => {
    const state = chatReducer(initialChatState, {
      type: 'loaded',
      detail: {
        ...detail,
        messages: [...detail.messages.slice(0, 2), { ...detail.messages[2]!, truncated: true }],
      },
    })
    expect(state.messages.at(-1)).toMatchObject({ id: 'm2', truncated: true })
  })
})
