import type { Locale } from '@/i18n'
import type { Copy } from './types'
import { en } from './en'
import { pt } from './pt'
import { es } from './es'

export type { Copy, PlanCopy, Feature, Step, Point } from './types'

/**
 * The landing page's languages.
 *
 * `Locale` is imported from `@/i18n` rather than redeclared — the app already
 * has a locale type and a storage key, and two independent notions of "what
 * language is this person reading" is how a product ends up in English in the
 * header and Portuguese in the menu.
 *
 * Adding a language: write `copy/<locale>.ts`, add it to `Locale` in
 * `src/i18n/index.ts`, then add one line to each map below. The type system
 * finds every string you have not translated yet.
 */
export const DICTIONARIES: Record<Locale, Copy> = { en, pt, es }

/**
 * The order the language menu lists them in.
 *
 * Not `SUPPORTED_LOCALES`, which is the app's list and starts with
 * Portuguese because that is the product's own default. This page leads in
 * English, so its menu does too — and menu order is a presentation decision
 * that belongs here rather than in shared config.
 */
export const LOCALE_ORDER: readonly Locale[] = ['en', 'pt', 'es']

/** What the language menu shows. Endonyms — people look for their own word. */
export const LOCALE_LABELS: Record<Locale, { name: string; english: string; code: string }> = {
  en: { name: 'English', english: 'English', code: 'EN' },
  pt: { name: 'Português', english: 'Portuguese', code: 'PT' },
  es: { name: 'Español', english: 'Spanish', code: 'ES' },
}

/**
 * The landing page defaults to English while the product defaults to
 * Portuguese. That is deliberate, not an inconsistency: the public page sells
 * internationally and is what a stranger sees first, and its server-rendered
 * HTML — the version search engines and link previews read — has to be in one
 * language. A visitor's own language is applied as soon as the page hydrates,
 * and their choice is remembered from then on.
 */
export const MARKETING_DEFAULT_LOCALE: Locale = 'en'

export function dictionaryFor(locale: Locale): Copy {
  return DICTIONARIES[locale] ?? DICTIONARIES[MARKETING_DEFAULT_LOCALE]
}
