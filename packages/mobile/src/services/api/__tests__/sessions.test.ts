import { configureApiClient } from '../client'
import { streamMessage } from '../sessions'
import { transportFetch, type TransportInit } from '../transport'
import { useLocaleStore } from '@/i18n'
import { frame, jsonResponse, sseResponse } from '@/test/transport'

jest.mock('../transport', () => ({ transportFetch: jest.fn() }))
const mockFetch = transportFetch as jest.MockedFunction<typeof transportFetch>

beforeEach(() => {
  mockFetch.mockReset()
  configureApiClient({ baseUrl: 'https://core.test', getToken: () => 'tok', onUnauthorized: jest.fn() })
})

describe('streamMessage', () => {
  it('posts to the unified /message endpoint with an Idempotency-Key and renders the chat pipeline', async () => {
    mockFetch.mockResolvedValueOnce(
      sseResponse([
        frame({ type: 'status', key: 'activity.chat.processing', message: 'Processando' }),
        frame({ type: 'text', content: 'Olá' }),
        frame({ type: 'text', content: ', mundo' }),
        frame({ type: 'done', session_id: 's1', message_id: 'm2', user_message_id: 'm1', usage: { input_tokens: 1, output_tokens: 2, cost_usd: 0, tcu: 0, duration_ms: 3 } }),
        'data: [DONE]\n\n',
      ]),
    )
    const onStatus = jest.fn()
    let text = ''

    const outcome = await streamMessage('s1', 'oi', { onStatus, onText: (chunk) => (text += chunk) }, { idempotencyKey: 'key-1' })

    const [url, init] = mockFetch.mock.calls[0] as [string, TransportInit]
    expect(url).toBe('https://core.test/sessions/s1/message')
    expect(init.headers['Idempotency-Key']).toBe('key-1')
    expect(init.headers.Accept).toBe('text/event-stream')
    // The app's language rides with every message; the Core answers in it (features/settings/language.ts).
    expect(JSON.parse(init.body!)).toEqual({ content: 'oi', locale: useLocaleStore.getState().locale })
    expect(onStatus).toHaveBeenCalledWith('activity.chat.processing', 'Processando')
    expect(text).toBe('Olá, mundo')
    expect(outcome).toMatchObject({ kind: 'done', event: { message_id: 'm2', user_message_id: 'm1' } })
  })

  it('renders the agent pipeline: tool steps, approval gate and the answer carried on done', async () => {
    mockFetch.mockResolvedValueOnce(
      sseResponse([
        frame({ type: 'tool_call', tool: 'web_search', args: { q: 'x' } }),
        frame({ type: 'approval_required', action_id: 'a1', action: 'write_file', args: {}, description: 'Salvar arquivo' }),
        frame({ type: 'tool_result', tool: 'web_search', success: true, output: 'ok' }),
        frame({ type: 'done', session_id: 's1', message_id: 'm9', answer: 'Resposta final', iterations: 2 }),
      ]),
    )
    const steps: string[] = []
    const onApprovalRequired = jest.fn()
    let text = ''

    const outcome = await streamMessage(
      's1',
      'pesquise',
      { onAgentStep: (step) => steps.push(`${step.type}:${step.tool}`), onApprovalRequired, onText: (chunk) => (text += chunk) },
      { idempotencyKey: 'k' },
    )

    expect(steps).toEqual(['tool_call:web_search', 'tool_result:web_search'])
    expect(onApprovalRequired).toHaveBeenCalledWith(expect.objectContaining({ action_id: 'a1' }))
    expect(text).toBe('Resposta final')
    expect(outcome.kind).toBe('done')
  })

  it('delivers creation progress and results from artifact events, dropping malformed ones', async () => {
    mockFetch.mockResolvedValueOnce(
      sseResponse([
        frame({ type: 'artifact', artifact: { id: 'img1', kind: 'image', status: 'running', progress: 0.4, provider: 'x' } }),
        frame({ type: 'artifact', artifact: { id: 'img1', kind: 'image', status: 'completed', preview_url: 'https://cdn.test/img1.png' } }),
        frame({ type: 'artifact', artifact: { kind: 'image' } }),
        frame({ type: 'done', session_id: 's1', message_id: 'm3' }),
      ]),
    )
    const media: { status: string; previewUrl: string | null }[] = []
    const outcome = await streamMessage('s1', 'crie uma imagem', { onMedia: (item) => media.push(item) }, { idempotencyKey: 'k' })

    expect(media.map((item) => item.status)).toEqual(['running', 'completed'])
    expect(media[1]?.previewUrl).toBe('https://cdn.test/img1.png')
    expect(JSON.stringify(media)).not.toContain('provider')
    expect(outcome.kind).toBe('done')
  })

  it('resolves an in-stream error with its code', async () => {
    mockFetch.mockResolvedValueOnce(
      sseResponse([
        frame({ type: 'status', message: 'Processando' }),
        frame({ type: 'error', error: 'quality_not_entitled', message: 'Upgrade your plan to access it.' }),
      ]),
    )
    await expect(streamMessage('s1', 'x', {}, { idempotencyKey: 'k' })).resolves.toEqual({
      kind: 'error',
      message: 'Seu plano atual não inclui este recurso.',
      code: 'quality_not_entitled',
    })
  })

  it('shows the Core generic in-stream failure in Portuguese', async () => {
    mockFetch.mockResolvedValueOnce(sseResponse([frame({ type: 'error', message: 'An internal error occurred.' })]))
    await expect(streamMessage('s1', 'x', {}, { idempotencyKey: 'k' })).resolves.toMatchObject({
      kind: 'error',
      message: 'O TRINDATRON encontrou um erro. Tente novamente.',
    })
  })

  it('reports an interrupted turn when the connection drops before done', async () => {
    mockFetch.mockResolvedValueOnce(sseResponse([frame({ type: 'text', content: 'parcial' })]))
    await expect(streamMessage('s1', 'x', {}, { idempotencyKey: 'k' })).resolves.toEqual({ kind: 'interrupted' })
  })

  it('understands an idempotent replay answered as JSON', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(200, { idempotent_replay: true, session_id: 's1', message_id: 'm5', status: 'completed' }, { 'content-type': 'application/json' }),
    )
    await expect(streamMessage('s1', 'x', {}, { idempotencyKey: 'k' })).resolves.toEqual({ kind: 'replay', sessionId: 's1', messageId: 'm5' })
  })

  it('rejects with a typed quota error before any stream starts', async () => {
    mockFetch.mockResolvedValueOnce(
      jsonResponse(429, { detail: { error: 'daily_limit_exceeded', message: 'Limite diário atingido.', resets_at: '2026-09-14T00:00:00Z' } }),
    )
    await expect(streamMessage('s1', 'x', {}, { idempotencyKey: 'k' })).rejects.toMatchObject({
      kind: 'quota_exceeded',
      code: 'daily_limit_exceeded',
    })
  })

  it('returns aborted when the user stops generation mid-stream', async () => {
    const controller = new AbortController()
    mockFetch.mockResolvedValueOnce(sseResponse([frame({ type: 'text', content: 'a' })], { close: false }))
    const outcome = await streamMessage('s1', 'x', { onText: () => controller.abort() }, { idempotencyKey: 'k', signal: controller.signal })
    expect(outcome).toEqual({ kind: 'aborted' })
  })
})

describe('the language each message carries', () => {
  afterEach(() => useLocaleStore.setState({ preference: 'auto', locale: useLocaleStore.getState().systemLocale }))

  it('is the language the app is showing, including one chosen in the app over the phone', async () => {
    useLocaleStore.setState({ preference: 'es', systemLocale: 'en', locale: 'es' })
    mockFetch.mockResolvedValueOnce(sseResponse(['data: [DONE]\n\n']))
    await streamMessage('s1', 'Você faz busca na internet?', {}, { idempotencyKey: 'k' })
    const [, init] = mockFetch.mock.calls[0] as [string, TransportInit]
    expect(JSON.parse(init.body!).locale).toBe('es')
  })
})
