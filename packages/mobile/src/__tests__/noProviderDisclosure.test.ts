import fs from 'fs'
import path from 'path'

import { jobResultText } from '@/features/agents/status'
import { t } from '@/i18n'
import { errorFromResponse, streamErrorMessage } from '@/services/api/errors'
import { containsInternalDetail, safeUserMessage } from '@/services/api/redact'

import en from '../i18n/locales/en.json'
import es from '../i18n/locales/es.json'
import pt from '../i18n/locales/pt.json'

const ROOT = path.resolve(__dirname, '../..')
const PROVIDER_NAMES = /\b(gpt|chatgpt|openai|anthropic|claude|gemini|mistral|grok|groq|llama|deepseek|elevenlabs|litellm|ollama)\b/i

function walk(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name)
    return entry.isDirectory() ? walk(full) : [full]
  })
}

function values(node: object): string[] {
  return Object.values(node).flatMap((value) => (typeof value === 'object' && value !== null ? values(value) : [String(value)]))
}

describe('the user only ever meets TRINDATRON', () => {
  it('recognises provider, model and infrastructure details', () => {
    for (const leak of [
      'Voice provider is missing required configuration: GEMINI_API_KEY',
      'litellm.RateLimitError: OpenAI rate limit reached for gpt-4o',
      'Claude Sonnet is overloaded',
      'Falling back to mistral-large',
      'model_id=grok-2 failed',
      'Traceback (most recent call last)',
    ]) {
      expect(containsInternalDetail(leak)).toBe(true)
    }
    for (const safe of ['Limite semanal atingido.', 'Sessão ativa', 'Relatório pronto para revisão', 'Your session has ended.']) {
      expect(containsInternalDetail(safe)).toBe(false)
    }
  })

  it('replaces a leaking Core error with the neutral message for its kind', () => {
    const error = errorFromResponse(424, { detail: 'Voice provider is missing required configuration: ELEVENLABS_API_KEY' })
    expect(error.message).toBe(t('error.api.dependency_unavailable'))
    expect(error.message).not.toMatch(PROVIDER_NAMES)

    expect(errorFromResponse(503, { detail: "No voice provider registered for 'elevenlabs'" }).message).not.toMatch(PROVIDER_NAMES)
    expect(streamErrorMessage('litellm.APIError: Anthropic returned 529', null)).toBe(t('error.api.server'))
    expect(safeUserMessage('gemini-2.5-pro timed out', 'fallback')).toBe('fallback')
  })

  it('shows an agent job result only when it is plain text that names nothing internal', () => {
    expect(jobResultText({ answer: 'Relatório pronto para revisão' })).toBe('Relatório pronto para revisão')
    expect(jobResultText({ answer: 'Gerado com GPT-4 via fallback' })).toBeNull()
    expect(jobResultText({ model: 'gpt-4o', provider: 'openai', tokens: 1200 })).toBeNull()
    expect(jobResultText(null)).toBeNull()
  })

  it('no translation in any language names a provider or model', () => {
    const leaks = [pt, en, es].flatMap(values).filter((text) => PROVIDER_NAMES.test(text))
    expect(leaks).toEqual([])
  })

  it('no app source string names a provider or model', () => {
    const files = [...walk(path.join(ROOT, 'app')), ...walk(path.join(ROOT, 'src'))].filter(
      (file) => /\.(ts|tsx)$/.test(file) && !/(__tests__|__integration__|redact\.ts$)/.test(file),
    )
    const leaks = files.flatMap((file) =>
      [...fs.readFileSync(file, 'utf8').matchAll(/(['"`])((?:(?!\1).)*)\1/g)]
        .map((match) => match[2] ?? '')
        .filter((literal) => PROVIDER_NAMES.test(literal))
        .map((literal) => `${path.relative(ROOT, file)}: ${literal}`),
    )
    expect(leaks).toEqual([])
  })
})
