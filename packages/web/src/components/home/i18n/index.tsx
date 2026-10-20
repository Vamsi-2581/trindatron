'use client'

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import appEn from '@/i18n/messages/en.json'
import appPt from '@/i18n/messages/pt.json'
import appEs from '@/i18n/messages/es.json'
import { en, type Dict } from './en'
import { pt } from './pt'
import { es } from './es'

export type Lang = 'en' | 'pt' | 'es'
export const LANGS: Lang[] = ['en', 'pt', 'es']
const DICTS: Record<Lang, Dict> = { en, pt, es }
const STORAGE_KEY = 'trd-home-lang'

interface LocaleApi {
  lang: Lang
  t: Dict
  setLang: (lang: Lang) => void
}

const LocaleContext = createContext<LocaleApi>({ lang: 'en', t: en, setLang: () => {} })
export const useLocale = () => useContext(LocaleContext)

/**
 * The Home speaks English by default, which is also what the server renders.
 * A visitor's explicit choice is remembered on this device and wins from then on.
 */
export function LocaleProvider({ children }: { children: React.ReactNode }) {
  const [lang, setLangState] = useState<Lang>('en')

  useEffect(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY) as Lang | null
      if (saved && LANGS.includes(saved)) setLangState(saved)
    } catch {
      // Storage can be unavailable (private windows); English stays.
    }
  }, [])

  useEffect(() => {
    document.documentElement.lang = DICTS[lang].meta.locale
    document.title = DICTS[lang].meta.title
  }, [lang])

  const setLang = useCallback((next: Lang) => {
    setLangState(next)
    try {
      localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // A preference that cannot persist still applies to this visit.
    }
  }, [])

  const api = useMemo(() => ({ lang, t: DICTS[lang], setLang }), [lang, setLang])
  return <LocaleContext.Provider value={api}>{children}</LocaleContext.Provider>
}

const APP: Record<Lang, Record<string, unknown>> = { en: appEn, pt: appPt, es: appEs }

/** The product's own wording for an activity key ("activity.tool.web_search"), in the visitor's language. */
export function activityLabel(key: string | undefined, lang: Lang): string {
  if (!key) return ''
  const walk = (dict: Record<string, unknown>) =>
    key.split('.').reduce<unknown>((node, part) => (node && typeof node === 'object' ? (node as Record<string, unknown>)[part] : undefined), dict)
  const hit = walk(APP[lang]) ?? walk(APP.en)
  return typeof hit === 'string' ? hit : ''
}
