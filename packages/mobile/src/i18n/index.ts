import AsyncStorage from '@react-native-async-storage/async-storage'
import * as Localization from 'expo-localization'
import { useCallback } from 'react'
import { create } from 'zustand'

import en from './locales/en.json'
import es from './locales/es.json'
import pt from './locales/pt.json'

/**
 * Same philosophy as packages/web/src/i18n: pt/en/es, dot-notation keys,
 * fallback to pt and then to the key itself, and the `trinity_locale`
 * storage key. The Mobile adds an explicit "auto" preference that follows
 * the operating system.
 */

export type Locale = 'pt' | 'en' | 'es'
export type LanguagePreference = 'auto' | Locale
export type TranslationParams = Record<string, string | number>

export const SUPPORTED_LOCALES: readonly Locale[] = ['pt', 'en', 'es']
export const DEFAULT_LOCALE: Locale = 'pt'
export const LOCALE_STORAGE_KEY = 'trinity_locale'

export const INTL_LOCALE: Record<Locale, string> = { pt: 'pt-BR', en: 'en-US', es: 'es-ES' }

interface Dictionary {
  [key: string]: string | Dictionary
}

const dictionaries: Record<Locale, Dictionary> = { pt, en, es }

export function localeFromLanguageTag(tag: string | null | undefined): Locale {
  const language = tag?.split(/[-_]/)[0]?.toLowerCase()
  if (language === 'en') return 'en'
  if (language === 'es') return 'es'
  return DEFAULT_LOCALE
}

export function detectSystemLocale(): Locale {
  try {
    const [preferred] = Localization.getLocales()
    return localeFromLanguageTag(preferred?.languageTag ?? preferred?.languageCode)
  } catch {
    return DEFAULT_LOCALE
  }
}

export function resolveLocale(preference: LanguagePreference, systemLocale: Locale): Locale {
  return preference === 'auto' ? systemLocale : preference
}

function isPreference(value: string): value is LanguagePreference {
  return value === 'auto' || (SUPPORTED_LOCALES as readonly string[]).includes(value)
}

function lookup(dictionary: Dictionary, key: string): string | undefined {
  let node: string | Dictionary | undefined = dictionary
  for (const part of key.split('.')) {
    if (typeof node !== 'object' || node === null) return undefined
    node = node[part]
  }
  return typeof node === 'string' ? node : undefined
}

export function hasTranslation(key: string, locale: Locale = DEFAULT_LOCALE): boolean {
  return lookup(dictionaries[locale], key) !== undefined
}

export function translate(locale: Locale, key: string, params?: TranslationParams): string {
  const template = lookup(dictionaries[locale], key) ?? lookup(dictionaries[DEFAULT_LOCALE], key) ?? key
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (match, name: string) => (name in params ? String(params[name]) : match))
}

interface LocaleState {
  preference: LanguagePreference
  systemLocale: Locale
  locale: Locale
  hydrated: boolean
  hydrate(): Promise<void>
  setPreference(preference: LanguagePreference): Promise<void>
  refreshSystemLocale(): void
}

const initialSystemLocale = detectSystemLocale()

export const useLocaleStore = create<LocaleState>()((set, get) => ({
  preference: 'auto',
  systemLocale: initialSystemLocale,
  locale: initialSystemLocale,
  hydrated: false,

  async hydrate() {
    let preference: LanguagePreference = 'auto'
    try {
      const stored = await AsyncStorage.getItem(LOCALE_STORAGE_KEY)
      if (stored && isPreference(stored)) preference = stored
    } catch {
      // Unreadable storage: follow the system language.
    }
    const systemLocale = detectSystemLocale()
    set({ preference, systemLocale, locale: resolveLocale(preference, systemLocale), hydrated: true })
  },

  async setPreference(preference) {
    set({ preference, locale: resolveLocale(preference, get().systemLocale) })
    try {
      await AsyncStorage.setItem(LOCALE_STORAGE_KEY, preference)
    } catch {
      // The choice still applies for this session.
    }
  },

  refreshSystemLocale() {
    const systemLocale = detectSystemLocale()
    set((state) => ({ systemLocale, locale: resolveLocale(state.preference, systemLocale) }))
  },
}))

/** For code outside React (errors, formatting). Components use useT(). */
export function t(key: string, params?: TranslationParams): string {
  return translate(useLocaleStore.getState().locale, key, params)
}

export function currentIntlLocale(): string {
  return INTL_LOCALE[useLocaleStore.getState().locale]
}

export function useLocale(): Locale {
  return useLocaleStore((state) => state.locale)
}

export function useT(): (key: string, params?: TranslationParams) => string {
  const locale = useLocale()
  return useCallback((key: string, params?: TranslationParams) => translate(locale, key, params), [locale])
}
