import { useLocaleStore } from '@/i18n'

import { ApiError, errorFromResponse, fallbackMessage, messageFromDetail, toApiError } from '../errors'

beforeEach(() => {
  useLocaleStore.setState({ preference: 'auto', systemLocale: 'pt', locale: 'pt' })
})

describe('errorFromResponse', () => {
  it('maps a structured 429 quota rejection with its renewal, in the user language', () => {
    const resetsAt = new Date(Date.now() + 2 * 86_400_000).toISOString()
    const error = errorFromResponse(429, {
      detail: { error: 'weekly_limit_exceeded', message: 'Limite semanal atingido. Seu acesso será renovado em 2 dias.', resets_at: resetsAt },
    })
    expect(error).toMatchObject({ kind: 'quota_exceeded', status: 429, code: 'weekly_limit_exceeded', resetsAt })
    expect(error.message).toBe('Você atingiu o limite de uso do seu plano. Seu acesso renova em 2 dias.')

    useLocaleStore.setState({ locale: 'en' })
    expect(errorFromResponse(429, { detail: { error: 'weekly_limit_exceeded', resets_at: resetsAt } }).message).toBe(
      "You've reached your plan's usage limit. Your access renews in 2 days.",
    )
  })

  it('maps a plain-string 429 as a request rate limit and translates known Core text', () => {
    const error = errorFromResponse(429, { detail: 'Too many login attempts. Please wait and try again.' })
    expect(error).toMatchObject({ kind: 'rate_limited', message: 'Muitas tentativas de login. Aguarde um pouco e tente novamente.' })
  })

  it('reads FastAPI validation arrays', () => {
    const error = errorFromResponse(422, { detail: [{ loc: ['body', 'password'], msg: 'Value error, Password must be at least 8 characters' }] })
    expect(error).toMatchObject({ kind: 'validation', message: 'A senha precisa ter pelo menos 8 caracteres.' })
  })

  it('never shows untranslated Core free text: it uses the message for the error kind', () => {
    expect(errorFromResponse(404, { detail: 'Session not found' }).message).toBe(fallbackMessage('not_found'))
    expect(errorFromResponse(403, { detail: { message: 'Agent mode requires PRO.' } }).message).toBe(fallbackMessage('forbidden'))
    useLocaleStore.setState({ locale: 'es' })
    expect(errorFromResponse(404, { detail: 'Session not found' }).message).toBe('No encontramos lo que buscabas.')
  })

  it('maps every status family', () => {
    expect(errorFromResponse(401, { detail: 'Invalid or expired token' }).kind).toBe('unauthorized')
    expect(errorFromResponse(409, { detail: 'busy' }).kind).toBe('conflict')
    expect(errorFromResponse(413, { detail: 'File exceeds 10 MB limit' }).kind).toBe('payload_too_large')
    expect(errorFromResponse(415, {}).kind).toBe('unsupported_media')
    expect(errorFromResponse(424, { detail: 'missing config' }).kind).toBe('dependency_unavailable')
    expect(errorFromResponse(503, {}).kind).toBe('dependency_unavailable')
    expect(errorFromResponse(500, { detail: 'Traceback (most recent call last) ...' })).toMatchObject({
      kind: 'server',
      message: fallbackMessage('server'),
    })
  })
})

describe('helpers', () => {
  it('messageFromDetail ignores unknown shapes', () => {
    expect(messageFromDetail(42)).toBeNull()
    expect(messageFromDetail([])).toBeNull()
  })

  it('toApiError wraps unknown failures as network errors and keeps ApiErrors', () => {
    const existing = new ApiError({ kind: 'timeout', status: null, message: 'x' })
    expect(toApiError(existing)).toBe(existing)
    expect(toApiError(new TypeError('Network request failed'))).toMatchObject({ kind: 'network', message: fallbackMessage('network') })
  })
})
