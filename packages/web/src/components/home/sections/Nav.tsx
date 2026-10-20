'use client'

import { useEffect, useState } from 'react'
import { BrandSymbol } from '@/components/brand/master'
import { LANGS, useLocale, type Lang } from '../i18n'
import s from '../home.module.css'

export function Nav({ signedIn, onSignIn, onTry }: { signedIn: boolean; onSignIn: () => void; onTry: () => void }) {
  const { t, lang, setLang } = useLocale()
  const [solid, setSolid] = useState(false)

  useEffect(() => {
    const onScroll = () => setSolid(window.scrollY > 24)
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  return (
    <header className={`${s.nav} ${solid ? s.navSolid : ''}`} data-no-stage>
      <a href="#top" className={s.brand} aria-label={t.nav.home}>
        <BrandSymbol size={30} priority />
        <span className={s.brandWord}>TRINDATRON</span>
      </a>
      <div className={s.navRight}>
        <div className={s.lang} role="group" aria-label={t.nav.language}>
          {LANGS.map((code: Lang) => (
            <button
              key={code}
              type="button"
              lang={code}
              aria-pressed={lang === code}
              onClick={() => setLang(code)}
              title={{ en: 'English', pt: 'Português', es: 'Español' }[code]}
            >
              {code.toUpperCase()}
            </button>
          ))}
        </div>
        {signedIn ? (
          <a href="/home" className={s.navLink}>{t.nav.workspace}</a>
        ) : (
          <button type="button" className={s.navLink} onClick={onSignIn}>{t.nav.signIn}</button>
        )}
        <button type="button" className={s.navCta} onClick={onTry}>{t.nav.tryFree}</button>
      </div>
    </header>
  )
}
