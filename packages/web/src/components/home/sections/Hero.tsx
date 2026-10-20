'use client'

import { useState } from 'react'
import { ArrowDown } from 'lucide-react'
import { BrandSymbol } from '@/components/brand/master'
import { useLocale } from '../i18n'
import { useStage } from '../stage/Stage'
import s from '../home.module.css'

/**
 * First screen: the promise, one field, and the living symbol on the stage
 * behind it. Whatever is typed here goes to the real conversation below.
 */
export function Hero({ onAsk }: { onAsk: (text: string) => void }) {
  const { t } = useLocale()
  const { pulse, mode } = useStage()
  const [text, setText] = useState('')

  return (
    <section id="top" className={s.hero} data-chapter="hero" aria-labelledby="hero-title">
      <div className={s.heroCopy}>
        <p className={s.heroEra}>{t.hero.era}</p>
        <h1 id="hero-title" className={s.heroTitle}>
          <span>{t.hero.h1a}</span>
          <span className={s.heroTitleB}>{t.hero.h1b}</span>
        </h1>
        <p className={s.heroSub}>{t.hero.sub}</p>

        <form
          className={s.ask}
          onSubmit={(e) => {
            e.preventDefault()
            onAsk(text)
            setText('')
          }}
        >
          <label htmlFor="hero-ask" className={s.srOnly}>{t.hero.placeholder}</label>
          <input
            id="hero-ask"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={t.hero.placeholder}
            autoComplete="off"
            enterKeyHint="send"
          />
          <button type="submit" className={s.askButton}>
            <span className={s.askLabel}>{t.hero.cta}</span>
            <span className={s.askMark}><BrandSymbol size={21} /></span>
          </button>
        </form>
        <p className={s.heroNote}>{t.hero.note}</p>
      </div>

      <div className={s.heroFoot}>
        <a href="#film" className={s.watch}>
          <ArrowDown size={16} aria-hidden="true" /> {t.hero.watch}
        </a>
        {mode === 'webgl' ? (
          <button type="button" className={s.hint} onClick={pulse}>
            <span className={s.srOnly}>{t.hero.wake}. </span>
            <span aria-hidden="true">{t.hero.hint}</span>
          </button>
        ) : null}
      </div>

      {mode === 'fallback' && (
        <div className={s.heroStill} aria-hidden="true">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/brand/master/source/trindatron-lockup-vertical.jpg" alt="" />
        </div>
      )}
    </section>
  )
}
