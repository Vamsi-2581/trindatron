/**
 * The Home shows what TRINDATRON can actually do for this account.
 *
 * What is guarded here is the decision made while editing, not the pixels: a
 * capability listed because it would be nice, a plan state decided in the
 * browser, a card for a product that does not exist, a link to a route that
 * was never built, or a "suggestion" that is only text. The Home this replaced
 * had four fixed prompts and two cards — TRINITY CODE and TRINITY WORK — whose
 * badges said "Em breve" or "Bloqueado" depending on a plan flag read here.
 */
import { readFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const root = join(__dirname, '..')
const read = (...parts: string[]) => readFileSync(join(root, ...parts), 'utf-8')
const home = read('app', '(app)', 'home', 'page.tsx')

const PROVIDERS = /openai|anthropic|claude|gemini|mistral|groq|\bxai\b|grok|litellm|deepseek|\bTCU\b/i

describe('the Home derives its actions from the Core', () => {
  it('asks the Core what this account can do', () => {
    expect(home).toContain('apiGetCapabilityMatrix')
  })

  it('decides nothing about plans by itself', () => {
    // The old Home read entitlement flags to choose between "Em breve" and "Bloqueado".
    expect(home).not.toMatch(/supports_trinity_code|supports_trinity_work/)
    expect(home).not.toMatch(/entitlement\.features/)
  })

  it('shows an action only when the matrix says it is available', () => {
    expect(home).toMatch(/usable\s*=\s*ACTIONS\.filter\(\(action\) => known\(action\)\?\.available\)/)
  })

  it('reports a real reason when something is not usable, and never a fake promise', () => {
    expect(home).toMatch(/plan:\s*'Não incluído no seu plano'/)
    expect(home).toMatch(/unavailable:\s*'Indisponível agora'/)
    expect(home).not.toMatch(/Em breve|EM BREVE|Bloqueado/)
  })
})

describe('no legacy remains on the Home', () => {
  it('does not advertise workspaces as separate products', () => {
    expect(home).not.toMatch(/TRINITY CODE|TRINITY WORK/)
  })

  it('dropped the four fixed prompts', () => {
    for (const old of [
      'Pesquise os últimos avanços em inteligência artificial generativa',
      'Escreva um script Python para analisar e visualizar dados CSV',
      'Crie um plano de marketing para o lançamento de um produto SaaS',
      'Revise este código e aponte vulnerabilidades de segurança',
    ]) {
      // The research one survives only as a real web-search request, not as a generic prompt.
      expect(home.includes(old)).toBe(false)
    }
  })

  it('names no provider, model or internal unit', () => {
    expect(home).not.toMatch(PROVIDERS)
  })
})

describe('every action starts something real', () => {
  it('sends, composes or opens — never nothing', () => {
    const block = home.slice(home.indexOf('const ACTIONS'), home.indexOf('const STATUS_NOTE'))
    const kinds = [...block.matchAll(/kind:\s*'(send|compose|open)'/g)].map((m) => m[1])
    expect(kinds.length).toBeGreaterThanOrEqual(6)
    expect(new Set(kinds)).toEqual(new Set(['send', 'compose', 'open']))
  })

  it('a research request actually asks for a web search with sources', () => {
    expect(home).toMatch(/Pesquise na web[^']*fontes/)
  })

  it('opens only routes that exist', () => {
    const hrefs = [...home.matchAll(/href:\s*'([^']+)'/g)].map((m) => m[1])
    expect(hrefs.length).toBeGreaterThan(0)
    for (const href of hrefs) {
      const page = join(root, 'app', '(app)', href.replace(/^\//, ''), 'page.tsx')
      expect(existsSync(page), `${href} has no page`).toBe(true)
    }
  })
})
