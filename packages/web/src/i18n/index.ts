/**
 * TRINITY i18n foundation — lightweight key-based translation.
 *
 * Design principles:
 * - No external library dependency for the foundation.
 * - Dot-notation keys (e.g. "activity.research.searching").
 * - Falls back: requested locale → pt (default) → key string itself.
 * - Locale stored in localStorage; auto-detected from browser on first visit.
 * - SSR-safe: all window/localStorage access is guarded.
 *
 * Extending to a new locale: add messages/<locale>.json and include it below.
 * Migrating to next-intl or react-i18next: replace this file — the key schema
 * and message files remain valid.
 */

export type Locale = 'pt' | 'en' | 'es'

export const SUPPORTED_LOCALES: readonly Locale[] = ['pt', 'en', 'es']
export const DEFAULT_LOCALE: Locale = 'pt'

const STORAGE_KEY = 'trinity_locale'

import pt from './messages/pt.json'
import en from './messages/en.json'
import es from './messages/es.json'

const dictionaries: Record<Locale, Record<string, unknown>> = { pt, en, es }

/**
 * Detect the user's preferred locale from browser language settings.
 * Returns a supported locale, defaulting to pt.
 */
function detectBrowserLocale(): Locale {
  if (typeof navigator === 'undefined') return DEFAULT_LOCALE
  const lang = navigator.language.split('-')[0].toLowerCase()
  if (lang === 'es') return 'es'
  if (lang === 'en') return 'en'
  return DEFAULT_LOCALE
}

/**
 * Read the active locale. Priority: localStorage → browser language → pt.
 */
export function getLocale(): Locale {
  if (typeof window === 'undefined') return DEFAULT_LOCALE
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored && (SUPPORTED_LOCALES as string[]).includes(stored)) {
      return stored as Locale
    }
  } catch {
    // localStorage unavailable (private window, sandboxed iframe, etc.)
  }
  return detectBrowserLocale()
}

/**
 * Persist locale choice. Call when the user explicitly changes language.
 */
export function setLocale(locale: Locale): void {
  if (typeof window === 'undefined') return
  try {
    localStorage.setItem(STORAGE_KEY, locale)
  } catch {
    // ignore
  }
}

/**
 * Resolve a dot-notation key to a translated string for the active locale.
 *
 * Falls back in order: requested locale → pt → the key string itself.
 * This means unknown keys are always displayed (useful during development
 * when message files are incomplete) and old PT-BR strings from the
 * pre-i18n era are returned unchanged.
 *
 * @param key  Dot-notation key ("activity.research.searching") or a
 *             raw legacy string ("Pesquisando informações").
 * @param locale  Optional override; uses getLocale() when omitted.
 */
export function t(key: string, locale?: Locale): string {
  const loc = locale ?? getLocale()
  const result = resolve(key, dictionaries[loc]) ?? resolve(key, dictionaries[DEFAULT_LOCALE]) ?? key
  return result
}

function resolve(key: string, dict: Record<string, unknown>): string | null {
  const parts = key.split('.')
  let current: unknown = dict
  for (const part of parts) {
    if (current !== null && typeof current === 'object') {
      current = (current as Record<string, unknown>)[part]
    } else {
      return null
    }
  }
  return typeof current === 'string' ? current : null
}
