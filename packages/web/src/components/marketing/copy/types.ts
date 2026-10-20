/**
 * The shape of the landing page, in words.
 *
 * Every string a visitor can read lives in one of these dictionaries. The
 * type is exhaustive on purpose: adding a sentence to the page without
 * translating it is a compile error, not something discovered later by a
 * Spanish-speaking visitor reading half an English page.
 *
 * Adding a language is `copy/<locale>.ts` plus one line in `copy/index.ts`.
 * Nothing else in the page knows how many languages exist.
 */

export type Feature = { label: string; included: boolean }

export type PlanCopy = {
  /** Commercial position in one word — "Explore", "Build momentum". */
  tagline: string
  /** How much room the plan gives, in plain language. Never a TCU figure. */
  volume: string
  features: Feature[]
  cta: string
}

export type Step = { label: string; detail: string }
export type Point = { title: string; body: string }

export interface Copy {
  meta: {
    title: string
    description: string
    ogLocale: string
    htmlLang: string
  }

  nav: {
    product: string
    work: string
    agents: string
    reach: string
    intelligence: string
    pricing: string
    signIn: string
    startFree: string
    openMenu: string
    closeMenu: string
    language: string
    languageHint: string
    backToApp: string
  }

  hero: {
    kicker: string
    titleA: string
    titleB: string
    body: string
    primary: string
    secondary: string
    note: string
  }

  inAction: {
    eyebrow: string
    title: string
    body: string
    request: string
    steps: Step[]
    resultLabel: string
    resultBody: string
    resultMeta: string
  }

  // The competitive frame. Not an attack on anyone — the argument is that a
  // category exists beyond the assistant people already have open in a tab.
  newWay: {
    eyebrow: string
    title: string
    body: string
    fromLabel: string
    toLabel: string
    rows: { from: string; to: string }[]
  }

  intelligences: {
    eyebrow: string
    title: string
    body: string
    points: Point[]
    caption: string
    youLabel: string
    coreLabel: string
    fieldLabel: string
  }

  work: {
    eyebrow: string
    title: string
    body: string
    steps: Step[]
    controlTitle: string
    controlBody: string
    approve: string
    review: string
  }

  agents: {
    eyebrow: string
    title: string
    body: string
    points: Point[]
  }

  reach: {
    eyebrow: string
    title: string
    body: string
    liveLabel: string
    nextLabel: string
    live: Point[]
    next: Point[]
  }

  memory: {
    eyebrow: string
    title: string
    body: string
    points: Point[]
  }

  why: {
    eyebrow: string
    title: string
    body: string
    points: Point[]
  }

  pricing: {
    eyebrow: string
    title: string
    body: string
    perMonth: string
    free: string
    recommended: string
    plans: Record<'free' | 'start' | 'pro' | 'max' | 'team', PlanCopy>
    windowNote: string
    currencyNote: string
  }

  closing: {
    title: string
    body: string
    primary: string
    secondary: string
  }

  footer: {
    tagline: string
    product: string
    company: string
    signIn: string
    rights: string
  }
}
