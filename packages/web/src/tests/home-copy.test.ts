/**
 * Guards on what the public Home is allowed to say, in every language.
 *
 * Every visible string lives in `components/home/i18n/*`, so the dictionaries
 * are the complete surface of what the page asserts. These checks keep the
 * three languages in step and keep the page honest: no model or provider
 * names, no prices typed into the copy, and every capability described as
 * part of the product rather than as something to come.
 */
import { describe, it, expect } from 'vitest'
import { en } from '@/components/home/i18n/en'
import { pt } from '@/components/home/i18n/pt'
import { es } from '@/components/home/i18n/es'

const DICTS = { en, pt, es }

/** Every key path in a dictionary, arrays included by index. */
function paths(node: unknown, prefix = ''): string[] {
  if (Array.isArray(node)) return node.flatMap((n, i) => paths(n, `${prefix}[${i}]`))
  if (node && typeof node === 'object') return Object.entries(node).flatMap(([k, v]) => paths(v, prefix ? `${prefix}.${k}` : k))
  return [prefix]
}

function strings(node: unknown, out: string[] = []): string[] {
  if (typeof node === 'string') out.push(node)
  else if (Array.isArray(node)) node.forEach((n) => strings(n, out))
  else if (node && typeof node === 'object') Object.values(node).forEach((n) => strings(n, out))
  return out
}

describe('home languages', () => {
  it('has the same keys in every language', () => {
    const base = paths(en).filter((p) => !p.startsWith('soon.lines'))
    for (const [code, dict] of Object.entries(DICTS)) {
      const own = paths(dict).filter((p) => !p.startsWith('soon.lines'))
      expect(own, `key mismatch in ${code}`).toEqual(base)
    }
  })

  it('has no empty strings except where a language needs none', () => {
    for (const [code, dict] of Object.entries(DICTS)) {
      const empty = paths(dict).filter((p) => {
        const value = p.split(/\.|\[|\]/).filter(Boolean).reduce<unknown>((n, k) => (n as Record<string, unknown>)[k], dict)
        return value === '' && p !== 'replay.recordedIn'
      })
      expect(empty, `empty strings in ${code}`).toEqual([])
    }
  })
})

describe('home honesty', () => {
  // "llama" only as a model name ("Llama 3"), never the Spanish verb ("se llama").
  const PROVIDERS = /\b(openai|anthropic|claude|gpt|gemini|google ai|grok|xai|groq|mistral|deepseek|sonnet|opus|haiku)\b|\bllama[\s-]?\d/i

  it('never names a model or provider', () => {
    for (const [code, dict] of Object.entries(DICTS)) {
      for (const text of strings(dict)) expect(text, `${code}: "${text}"`).not.toMatch(PROVIDERS)
    }
  })

  it('never types a price into the copy (prices come from the catalogue)', () => {
    for (const [code, dict] of Object.entries(DICTS)) {
      for (const text of strings(dict)) expect(text, `${code}: "${text}"`).not.toMatch(/(R\$|US\$|\$|€)\s?\d/)
    }
  })

  it('presents every capability as part of the product, with no future-tense labels', () => {
    const future = /coming soon|arriving|in development|when it arrives|em breve|chegando|em desenvolvimento|quando chegar|próximamente|muy pronto|en desarrollo|cuando llegue/i
    for (const [code, dict] of Object.entries(DICTS)) {
      for (const text of strings(dict)) expect(text, `${code}: "${text}"`).not.toMatch(future)
    }
  })

  it('gives each of the ten studio capabilities its words, a plan and a described demo', () => {
    const rows = ['video', 'images', 'editing', 'motion', 'vision', 'avatar', 'slides', 'docs', 'computer', 'code'] as const
    for (const [code, dict] of Object.entries(DICTS)) {
      for (const k of rows) {
        const row = dict.studio[k]
        expect(row.kicker && row.t && row.d, `${code}.${k}`).toBeTruthy()
        expect(row.plan, `${code}.${k}`).toMatch(/START|PRO|plan/i)
      }
      for (const k of ['editing', 'motion', 'avatar', 'slides', 'docs', 'computer', 'code'] as const) expect(dict.studio[k].alt.length, `${code}.${k}.alt`).toBeGreaterThan(20)
      expect(dict.create.image.ready).toContain('{s}')
      expect(dict.create.video.ready).toContain('{s}')
    }
  })

  it('labels the film as a real, time-lapsed session', () => {
    for (const dict of Object.values(DICTS)) {
      expect(dict.film.caption).toMatch(/real/i)
      expect(dict.film.caption).toMatch(/2026/)
    }
  })
})
