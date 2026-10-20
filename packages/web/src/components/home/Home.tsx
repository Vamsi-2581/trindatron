'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { LocaleProvider, useLocale } from './i18n'
import { StageProvider, useStage } from './stage/Stage'
import type { ChapterId } from './stage/chapters'
import { gsap, ScrollTrigger } from './motion'
import { Chat, type Auth, type ChatHandle } from './chat/Chat'
import { Nav } from './sections/Nav'
import { Hero } from './sections/Hero'
import { HowItWorks } from './sections/HowItWorks'
import { Film } from './sections/Film'
import { Studio } from './sections/Studio'
import { Today } from './sections/Today'
import { Capabilities } from './sections/Capabilities'
import { Pricing } from './sections/Pricing'
import { BrandSymbol } from '@/components/brand/master'
import s from './home.module.css'

/**
 * The public TRINDATRON Home.
 *
 * Impact → explain → demonstrate → want to try → sign up → use → subscribe.
 * One fixed WebGL stage runs under the whole page and re-composes itself for
 * every chapter; the chapters scroll over it. The conversation at the end is
 * the real product (POST /sessions/{id}/message), gated by a real sign-up.
 */
export function Home({ fontClass }: { fontClass: string }) {
  return (
    <LocaleProvider>
      <StageProvider>
        <Page fontClass={fontClass} />
      </StageProvider>
    </LocaleProvider>
  )
}

function Page({ fontClass }: { fontClass: string }) {
  const { t, lang } = useLocale()
  const { setChapter } = useStage()
  const root = useRef<HTMLDivElement>(null)
  const chat = useRef<ChatHandle>(null)
  const [auth, setAuth] = useState<Auth>('checking')

  const toChat = useCallback(() => {
    document.getElementById('try')?.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' })
  }, [])

  const ask = useCallback((text: string) => {
    toChat()
    if (text.trim()) window.setTimeout(() => chat.current?.submit(text), 350)
    else window.setTimeout(() => chat.current?.focus(), 350)
  }, [toChat])

  const signIn = useCallback(() => { toChat(); window.setTimeout(() => chat.current?.openGate('login'), 350) }, [toChat])
  const choosePlan = useCallback((plan: string) => {
    if (auth === 'signed' && plan !== 'free') { window.location.href = '/plans'; return }
    toChat()
    window.setTimeout(() => (auth === 'signed' ? chat.current?.focus() : chat.current?.openGate('signup')), 350)
  }, [auth, toChat])

  // Chapters tell the stage what to compose; plain [data-reveal] elements rise into place once.
  useEffect(() => {
    const el = root.current
    if (!el) return
    const mm = gsap.matchMedia()
    const ctx = gsap.context(() => {
      el.querySelectorAll<HTMLElement>('[data-chapter]').forEach((section) => {
        ScrollTrigger.create({
          trigger: section,
          start: 'top 55%',
          end: 'bottom 45%',
          onToggle: (self) => { if (self.isActive) setChapter(section.dataset.chapter as ChapterId) },
        })
      })
      mm.add('(prefers-reduced-motion: no-preference)', () => {
        gsap.utils.toArray<HTMLElement>('[data-reveal]').forEach((node) => {
          gsap.from(node, { y: 36, autoAlpha: 0, duration: 1, ease: 'power3.out', scrollTrigger: { trigger: node, start: 'top 90%', once: true } })
        })
      })
    }, el)
    return () => { ctx.revert(); mm.revert() }
  }, [setChapter])

  // Text length changes with the language; scroll-linked chapters must re-measure.
  useEffect(() => { const id = window.setTimeout(() => ScrollTrigger.refresh(), 60); return () => window.clearTimeout(id) }, [lang])

  return (
    <div ref={root} className={`${s.home} ${fontClass}`}>
      <a href="#try" className={s.skip}>{t.nav.tryFree}</a>
      <Nav signedIn={auth === 'signed'} onSignIn={signIn} onTry={() => ask('')} />
      <main>
        <Hero onAsk={ask} />
        <Film />
        <Studio />
        <Today />
        <Capabilities />
        <HowItWorks />
        <Pricing onChoose={choosePlan} />

        <section id="try" className={s.tryIt} data-chapter="try" aria-labelledby="try-title">
          <header className={s.tryHead}>
            <p className={s.kicker} data-reveal>{t.chat.kicker}</p>
            <h2 id="try-title" className={s.h2Display} data-reveal>{t.chat.title}</h2>
            <p className={s.lead} data-reveal>{t.chat.sub}</p>
          </header>
          <Chat ref={chat} onAuthChange={setAuth} />
        </section>

        <section className={s.final} data-chapter="final" aria-labelledby="final-title">
          <h2 id="final-title" className={s.finalTitle} data-reveal>{t.final.title}</h2>
          <p className={s.finalSub} data-reveal>{t.final.sub}</p>
          <button type="button" className={s.btnPrimaryLg} onClick={() => ask('')} data-reveal>{t.final.cta}</button>
        </section>
      </main>

      <footer className={s.footer}>
        <span className={s.footerBrand}><BrandSymbol size={20} /> TRINDATRON</span>
        <nav aria-label={t.footer.nav} className={s.footerLinks}>
          <a href="#top">{t.footer.top}</a>
          <button type="button" onClick={signIn}>{t.footer.signIn}</button>
        </nav>
        <span>{t.footer.rights}</span>
      </footer>
    </div>
  )
}
