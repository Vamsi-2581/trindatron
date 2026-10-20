import { act, renderHook, waitFor } from '@testing-library/react-native'

import { ApiError } from '@/services/api/errors'
import * as sessions from '@/services/api/sessions'
import type { SessionDetail } from '@/services/api/types'

import { useChatSession } from '../useChatSession'

jest.mock('@/services/api/sessions')
const api = sessions as jest.Mocked<typeof sessions>

function detail(messages: SessionDetail['messages'] = []): SessionDetail {
  return { id: 's1', title: 'Conversa', created_at: '2026-09-13T10:00:00', updated_at: '2026-09-13T10:00:00', messages }
}

const userMessage = (id: string, content: string) => ({
  id,
  session_id: 's1',
  role: 'user' as const,
  content,
  created_at: '2026-09-13T10:00:00',
})

beforeEach(() => {
  jest.resetAllMocks()
  api.getSession.mockResolvedValue(detail())
})

async function mounted() {
  const hook = renderHook(() => useChatSession('s1'))
  await waitFor(() => expect(hook.result.current.state.phase).toBe('ready'))
  return hook
}

describe('useChatSession', () => {
  it('streams a turn through the Core and finalises it with server ids', async () => {
    api.streamMessage.mockImplementation(async (_id, _content, handlers) => {
      handlers.onStatus?.('activity.chat.processing', 'Processando')
      handlers.onText?.('Olá!')
      return { kind: 'done', event: { type: 'done', session_id: 's1', message_id: 'a1', user_message_id: 'u1' } }
    })
    const { result } = await mounted()

    await act(() => result.current.send('  oi  '))

    expect(api.streamMessage).toHaveBeenCalledWith('s1', 'oi', expect.any(Object), expect.objectContaining({ idempotencyKey: expect.any(String) }))
    expect(result.current.state.streaming).toBe(false)
    expect(result.current.state.messages).toEqual([
      expect.objectContaining({ id: 'u1', role: 'user', content: 'oi' }),
      expect.objectContaining({ id: 'a1', role: 'assistant', content: 'Olá!', state: 'final' }),
    ])
  })

  it('retry deletes the orphan user message the Core kept, then resends once', async () => {
    api.streamMessage
      .mockResolvedValueOnce({ kind: 'error', message: 'An internal error occurred.', code: null })
      .mockImplementationOnce(async (_id, _content, handlers) => {
        handlers.onText?.('Agora foi.')
        return { kind: 'done', event: { type: 'done', session_id: 's1', message_id: 'a2', user_message_id: 'u2' } }
      })
    api.deleteMessagesFrom.mockResolvedValue(undefined)
    const { result } = await mounted()

    await act(() => result.current.send('pergunta'))
    expect(result.current.state.turnError).toMatchObject({ content: 'pergunta', retryable: true })

    api.getSession.mockResolvedValueOnce(detail([userMessage('orphan', 'pergunta')]))
    await act(() => result.current.retry())

    expect(api.deleteMessagesFrom).toHaveBeenCalledWith('s1', 'orphan')
    expect(api.streamMessage).toHaveBeenCalledTimes(2)
    expect(result.current.state.turnError).toBeNull()
    expect(result.current.state.messages.map((message) => message.content)).toEqual(['pergunta', 'Agora foi.'])
  })

  it('retry does not delete anything when the failed message never reached the Core', async () => {
    api.streamMessage
      .mockRejectedValueOnce(new ApiError({ kind: 'network', status: null, message: 'Sem conexão' }))
      .mockResolvedValueOnce({ kind: 'done', event: { type: 'done', session_id: 's1', message_id: 'a3' } })
    const { result } = await mounted()

    await act(() => result.current.send('pergunta'))
    expect(result.current.state.turnError?.retryable).toBe(true)
    await act(() => result.current.retry())

    expect(api.deleteMessagesFrom).not.toHaveBeenCalled()
    expect(api.streamMessage).toHaveBeenCalledTimes(2)
  })

  it('does not offer retry for a quota rejection and keeps the Core message', async () => {
    api.streamMessage.mockRejectedValueOnce(
      new ApiError({ kind: 'quota_exceeded', status: 429, code: 'weekly_limit_exceeded', message: 'Limite semanal atingido.' }),
    )
    const { result } = await mounted()

    await act(() => result.current.send('mais uma'))

    expect(result.current.state.turnError).toEqual({
      message: 'Limite semanal atingido.',
      code: 'weekly_limit_exceeded',
      content: 'mais uma',
      retryable: false,
    })
  })

  it('cancel aborts the stream and leaves no empty answer behind', async () => {
    api.streamMessage.mockImplementation(
      (_id, _content, _handlers, options) =>
        new Promise((resolve) => options.signal?.addEventListener('abort', () => resolve({ kind: 'aborted' }))),
    )
    const { result } = await mounted()

    let sending: Promise<void> = Promise.resolve()
    act(() => {
      sending = result.current.send('longa tarefa')
    })
    await waitFor(() => expect(result.current.state.streaming).toBe(true))
    await act(async () => {
      result.current.cancel()
      await sending
    })

    expect(result.current.state.streaming).toBe(false)
    expect(result.current.state.messages.map((message) => message.role)).toEqual(['user'])
  })

  it('ignores a second send while a turn is streaming', async () => {
    let finish: (() => void) | null = null
    api.streamMessage.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = () => resolve({ kind: 'done', event: { type: 'done', session_id: 's1', message_id: 'a' } })
        }),
    )
    const { result } = await mounted()

    let first: Promise<void> = Promise.resolve()
    act(() => {
      first = result.current.send('um')
      void result.current.send('dois')
    })
    await act(async () => {
      finish?.()
      await first
    })
    expect(api.streamMessage).toHaveBeenCalledTimes(1)
  })

  it('resolves an agent approval gate through the Core', async () => {
    api.approveAgentAction.mockResolvedValue({ status: 'approved' })
    let finish: (() => void) | null = null
    api.streamMessage.mockImplementation(async (_id, _content, handlers) => {
      handlers.onApprovalRequired?.({ action_id: 'act-1', action: 'write_file', args: {}, description: 'Salvar relatório' })
      await new Promise<void>((resolve) => {
        finish = resolve
      })
      return { kind: 'done', event: { type: 'done', session_id: 's1', message_id: 'a', answer: 'feito' } }
    })
    const { result } = await mounted()

    let sending: Promise<void> = Promise.resolve()
    act(() => {
      sending = result.current.send('gere o relatório')
    })
    await waitFor(() => expect(result.current.state.approval?.action_id).toBe('act-1'))
    await act(() => result.current.resolveApproval(true))
    expect(api.approveAgentAction).toHaveBeenCalledWith('s1', 'act-1')
    expect(result.current.state.approval).toBeNull()

    await act(async () => {
      finish?.()
      await sending
    })
  })
})
