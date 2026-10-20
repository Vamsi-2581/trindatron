'use client'

import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { SUPPORTED_LOCALES, setLocale as persistLocale, type Locale } from '@/i18n'
import { dictionaryFor, MARKETING_DEFAULT_LOCALE, type Copy } from './copy'

type Ctx = { locale: Locale; copy: Copy; setLocale: (l: Locale) => void; ready: boolean }

const LocaleContext = createContext<Ctx>({
  locale: MARKETING_DEFAULT_LOCALE,
  copy: dictionaryFor(MARKETING_DEFAULT_LOCALE),
  setLocale: () => {},
  ready: false,
})

/**
 * Resolve a browser language tag to something we actually publish.
 *
 * Matches on the primary subtag, so pt-BR, pt-PT and plain pt all land on
 * Portuguese — a Brazilian visitor should not get English because their
 * region happens to carry a suffix.
 */
function detect(): Locale | null {
  if (typeof navigator === 'undefined') return null
  const tags = navigator.languages?.length ? navigator.languages : [navigator.language]
  for (const tag of tags) {
    const primary = tag?.split('-')[0]?.toLowerCase()
    if (primary && (SUPPORTED_LOCALES as readonly string[]).includes(primary)) {
      return primary as Locale
    }
  }
  return null
}

const STORAGE_KEY = 'trinity_locale'

function stored(): Locale | null {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    return v && (SUPPORTED_LOCALES as readonly string[]).includes(v) ? (v as Locale) : null
  } catch {
    return null
  }
}

/**
 * Language for the public page.
 *
 * The server always renders `MARKETING_DEFAULT_LOCALE`, so the HTML is stable,
 * cacheable and the same for every crawler. On hydration this switches to the
 * visitor's own language — their stored choice first, their browser second.
 * A previous explicit choice always outranks the browser: someone who picked
 * English on a Portuguese machine meant it.
 */
export function LocaleProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(MARKETING_DEFAULT_LOCALE)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    const next = stored() ?? detect() ?? MARKETING_DEFAULT_LOCALE
    setLocaleState(next)
    setReady(true)
  }, [])

  // Keep the document in sync so screen readers announce the right language
  // and the browser offers to translate the right thing.
  useEffect(() => {
    if (!ready) return
    document.documentElement.lang = dictionaryFor(locale).meta.htmlLang
  }, [locale, ready])

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next)
    persistLocale(next)
  }, [])

  return (
    <LocaleContext.Provider value={{ locale, copy: dictionaryFor(locale), setLocale, ready }}>
      {children}
    </LocaleContext.Provider>
  )
}

export function useCopy(): Copy {
  return useContext(LocaleContext).copy
}

export function useLocale() {
  const { locale, setLocale, ready } = useContext(LocaleContext)
  return { locale, setLocale, ready }
}
