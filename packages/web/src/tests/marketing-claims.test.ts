/**
 * Guards on what the public page is allowed to say — in every language.
 *
 * The landing page is the one place in this repo where a plausible sentence
 * can become a false claim about the product or its price, and it now ships
 * in three languages, so a claim can be true in English and wrong in Spanish.
 *
 * These read the copy dictionaries rather than the components. That is the
 * point: since the rebuild, every string a visitor can see lives in
 * `components/marketing/copy/*`, so a dictionary is the complete surface of
 * what the page asserts. Testing the rendered markup would test React.
 */
import { describe, it, expect } from 'vitest'
import { DICTIONARIES, LOCALE_LABELS, MARKETING_DEFAULT_LOCALE } from '@/components/marketing/copy'
import type { Copy } from '@/components/marketing/copy'
import { SUPPORTED_LOCALES, type Locale } from '@/i18n'

const LOCALES = SUPPORTED_LOCALES as readonly Locale[]
const each = LOCALES.map((l) => [l, DICTIONARIES[l]] as const)

/** Every human-readable string in a dictionary, flattened. */
function strings(node: unknown, out: string[] = []): string[] {
  if (typeof node === 'string') out.push(node)
  else if (Array.isArray(node)) node.forEach((n) => strings(n, out))
  else if (node && typeof node === 'object') Object.values(node).forEach((n) => strings(n, out))
  return out
}

const allText = (c: Copy) => strings(c).join('\n')

// ── Completeness ───────────────────────────────────────────────────────────

describe('languages', () => {
  it('publishes a dictionary for every supported locale', () => {
    for (const l of LOCALES) {
      expect(DICTIONARIES[l], `missing dictionary for ${l}`).toBeTruthy()
      expect(LOCALE_LABELS[l]?.name, `missing menu label for ${l}`).toBeTruthy()
    }
  })

  it('renders English on the server, so the crawled HTML is one language', () => {
    expect(MARKETING_DEFAULT_LOCALE).toBe('en')
  })

  it.each(each)('%s translates every string — no English left behind', (locale, copy) => {
    if (locale === 'en') return
    const en = allText(DICTIONARIES.en)
    const mine = strings(copy)
    // A few strings are legitimately identical across languages: the brand
    // name, and "Computer use" which is kept as a term of art.
    const SHARED = /^(TRINDATRON|Computer use)$/
    const untranslated = mine.filter((s) => s.length > 24 && en.includes(s) && !SHARED.test(s))
    expect(untranslated, `untranslated strings in ${locale}`).toEqual([])
  })

  it.each(each)('%s covers all five plans', (_l, copy) => {
    expect(Object.keys(copy.pricing.plans).sort()).toEqual(
      ['free', 'max', 'pro', 'start', 'team'].sort(),
    )
  })
})

// ── Prices: PLAN_PRICES in packages/core/src/billing/pricing.py ────────────

describe('pricing', () => {
  // Mirrors the pricing catalog. The backend guards these against the cost
  // model in tests/unit/test_plan_economics.py; this guards the shop window.
  const OFFICIAL = { start: 9.9, pro: 19.9, max: 79.9, team: 199.9 } as const

  // Published monthly rates of the products TRINDATRON is positioned against.
  const CEILING = { start: 10, pro: 20, max: 100, team: 200 } as const

  it.each(Object.entries(OFFICIAL))('never prices %s above its comparable', (plan, amount) => {
    expect(amount).toBeLessThanOrEqual(CEILING[plan as keyof typeof CEILING])
  })

  it('keeps the plan order ascending', () => {
    const amounts = Object.values(OFFICIAL)
    expect(amounts).toEqual([...amounts].sort((a, b) => a - b))
  })
})

// ── The unit the user must never see ──────────────────────────────────────

describe('internal accounting stays internal', () => {
  it.each(each)('%s never shows TCU or a raw quota figure', (_l, copy) => {
    const text = allText(copy)
    expect(text).not.toMatch(/\bTCU\b/i)
    expect(text).not.toMatch(/\btokens?\b/i)
    expect(text).not.toMatch(/compute units?/i)
  })
})

// ── Claims that must match the entitlement table ──────────────────────────

describe('plan claims match src/entitlements.py', () => {
  it.each(each)('%s gives FREE only what FREE actually has', (_l, copy) => {
    const free = copy.pricing.plans.free
    // supports_agent_mode / supports_trinity_code / supports_trinity_work are
    // all False for "free", and max_agents is 0 — so the card has to show
    // exclusions, and cannot read as a complete product.
    expect(free.features.some((f) => !f.included)).toBe(true)
    expect(free.features.filter((f) => f.included).length).toBeLessThanOrEqual(3)
  })

  it.each(each)('%s puts the renewing session on every paid plan and not on FREE', (_l, copy) => {
    // START, PRO, MAX and TEAM all carry session_tcu_limit; FREE carries None,
    // so only the paid plans have a window that renews.
    const renews = /5\s*h(ours|oras)?|cinco horas|five hours/i
    for (const id of ['start', 'pro', 'max', 'team'] as const) {
      const labels = copy.pricing.plans[id].features.map((f) => f.label).join(' ')
      expect(renews.test(labels), `${id} should mention the renewing session`).toBe(true)
    }
    const freeLabels = copy.pricing.plans.free.features.map((f) => f.label).join(' ')
    expect(renews.test(freeLabels), 'FREE has no session budget').toBe(false)
  })

  it.each(each)('%s reserves the premium tier for MAX and TEAM', (_l, copy) => {
    // The router caps PRO at "standard"; only max/team reach "premium".
    const premium = /premium/i
    expect(premium.test(copy.pricing.plans.pro.features.map((f) => f.label).join(' '))).toBe(false)
    expect(premium.test(copy.pricing.plans.start.features.map((f) => f.label).join(' '))).toBe(false)
    expect(premium.test(copy.pricing.plans.max.features.map((f) => f.label).join(' '))).toBe(true)
  })

  it.each(each)('%s never claims an unlimited allowance', (_l, copy) => {
    const text = allText(copy)
    expect(text).not.toMatch(/ilimitad[oa]s?|unlimited|sin l[íi]mite\b/i)
    expect(text).not.toMatch(/(sem|n[ãa]o t[êe]m|no) limite[s]? di[áa]ri/i)
  })
})

// ── Capabilities: only what is wired into a real run ──────────────────────

describe('capability claims', () => {
  it.each(each)('%s never says it operates your own computer', (_l, copy) => {
    // The desktop tools exist in packages/agent but are NOT registered by
    // agent_service.py, so nothing in production drives a user's machine.
    // Computer use is positioned as the agent's own environment.
    const text = allText(copy)
    expect(text).not.toMatch(/your (own )?(computer|desktop|machine|pc)\b/i)
    expect(text).not.toMatch(/seu (pr[óo]prio )?(computador|desktop|pc)\b/i)
    expect(text).not.toMatch(/tu (propio )?(ordenador|equipo|pc)\b/i)
    expect(text).not.toMatch(/controls? your screen|controla sua tela/i)
  })

  it.each(each)('%s separates what runs today from what is next', (_l, copy) => {
    expect(copy.reach.live.length).toBeGreaterThan(0)
    expect(copy.reach.next.length).toBeGreaterThan(0)
    expect(copy.reach.liveLabel).toBeTruthy()
    expect(copy.reach.nextLabel).toBeTruthy()
  })
})

// ── Positioning: no provider names, no invented proof ─────────────────────

describe('positioning', () => {
  const VENDORS =
    /\b(anthropic|openai|chatgpt|gpt-?4|gpt-?5|claude|gemini|groq|xai|grok|mistral|llama|deepseek)\b/i

  it.each(each)('%s names no provider or model', (_l, copy) => {
    // The catalogue changes. A page that lists today's providers goes stale
    // on its own, and it asks the visitor to have an opinion about the one
    // decision the product exists to make for them.
    expect(allText(copy)).not.toMatch(VENDORS)
  })

  it.each(each)('%s invents no social proof', (_l, copy) => {
    const text = allText(copy)
    expect(text).not.toMatch(
      /\b\d[\d.,]*\s*(\+|mil|million|milh[õo]es|k\b)?\s*(users|usu[áa]rios|clientes|customers|companies|empresas)/i,
    )
    expect(text).not.toMatch(/trusted by|usado por|utilizado por|case stud|depoiment|testimonial/i)
    expect(text).not.toMatch(/\b(award|pr[êe]mio|premiad)/i)
  })
})

// ── Tone: the page sells, it does not defend ──────────────────────────────

describe('commercial language', () => {
  // Words that describe the product by what could go wrong. Each is a real
  // concept inside the product — failure handling, limits, refusals — and
  // each belongs in the app, the docs and the code, not on the page someone
  // reads before they have bought anything.
  const DEFENSIVE = [
    /\brisks?\b/i, /\bfailures?\b/i, /\bdangers?\b/i, /\bproblems?\b/i,
    /\berrors?\b/i, /\bunavailable\b/i, /\bblocked\b/i, /\blimitations?\b/i,
    /\bwe (don'?t|do not|cannot|can'?t)\b/i, /\bnot available\b/i,
    /\bno public (cases|customers)\b/i,
    /\brisco/i, /\bfalhas?\b/i, /\bperigo/i, /\bproblemas?\b/i,
    /\bindispon[íi]vel/i, /\bbloquead/i, /\blimita[çc][õo]es/i,
    /\bn[ãa]o temos\b/i, /\bn[ãa]o prometemos\b/i,
    /\briesgos?\b/i, /\bfallos?\b/i, /\bpeligro/i, /\bno tenemos\b/i,
  ]

  it.each(each)('%s describes the product by what it gives you', (locale, copy) => {
    const text = allText(copy)
    const hits = DEFENSIVE.filter((re) => re.test(text)).map((re) => String(re))
    expect(hits, `defensive language in ${locale}`).toEqual([])
  })

  it.each(each)('%s leads with a benefit, not an architecture note', (_l, copy) => {
    expect(copy.hero.titleA.length).toBeLessThanOrEqual(40)
    expect(copy.hero.titleB.length).toBeLessThanOrEqual(40)
    expect(copy.hero.primary.length).toBeLessThanOrEqual(24)
    expect(allText(copy)).not.toMatch(/router|roteador|enrutador|failover|fallback/i)
  })
})

// ── Conversion paths ──────────────────────────────────────────────────────

describe('calls to action', () => {
  it.each(each)('%s offers one primary action and keeps sign-in secondary', (_l, copy) => {
    expect(copy.hero.primary).toBeTruthy()
    expect(copy.closing.primary).toBe(copy.hero.primary)
    expect(copy.closing.secondary).toBe(copy.nav.signIn)
    expect(copy.footer.signIn).toBe(copy.nav.signIn)
  })

  it.each(each)('%s gives every plan its own call to action', (_l, copy) => {
    for (const id of ['free', 'start', 'pro', 'max', 'team'] as const) {
      expect(copy.pricing.plans[id].cta.length).toBeGreaterThan(0)
    }
  })
})
