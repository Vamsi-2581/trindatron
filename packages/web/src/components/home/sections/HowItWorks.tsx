'use client'

import { useEffect, useRef, useState } from 'react'
import { useLocale } from '../i18n'
import { useStage } from '../stage/Stage'
import type { ChapterId } from '../stage/chapters'
import { CINEMA, FLOW, gsap, ScrollTrigger } from '../motion'
import s from '../home.module.css'

const SCENES: ChapterId[] = ['say', 'works', 'done']

/**
 * "You speak. It works." in three beats. As the chapter passes through the
 * viewport each beat lights up and re-forms the stage (sound rings, many
 * minds, the finished glow); on phones the beats simply follow each other.
 */
export function HowItWorks() {
  const { t } = useLocale()
  const { setChapter } = useStage()
  const root = useRef<HTMLElement>(null)
  const steps = useRef<(HTMLLIElement | null)[]>([])
  const [active, setActive] = useState(0)

  useEffect(() => {
    const el = root.current
    if (!el) return
    const go = (i: number) => { setActive(i); setChapter(SCENES[i]) }
    const mm = gsap.matchMedia()
    // The beats follow the section's own pass through the viewport. Nothing is pinned: the wheel moves
    // the page as usual and the steps light up on the way.
    mm.add(CINEMA, () => {
      ScrollTrigger.create({
        trigger: el,
        start: 'top 55%',
        end: 'bottom 45%',
        onUpdate: (self) => go(Math.min(2, Math.floor(self.progress * 3 * 0.999))),
        onToggle: (self) => { if (self.isActive) go(Math.min(2, Math.floor(self.progress * 3 * 0.999))) },
      })
    })
    mm.add(FLOW, () => {
      steps.current.forEach((step, i) => {
        if (!step) return
        ScrollTrigger.create({ trigger: step, start: 'top 65%', end: 'bottom 35%', onToggle: (self) => { if (self.isActive) go(i) } })
      })
    })
    return () => mm.revert()
  }, [setChapter])

  return (
    <section ref={root} id="promise" className={s.promise} aria-labelledby="promise-title">
      <div className={s.promiseInner}>
        <p className={s.kicker}>{t.promise.kicker}</p>
        <h2 id="promise-title" className={s.h2}>{t.promise.title}</h2>
        <ol className={s.steps}>
          {t.promise.steps.map((step, i) => (
            <li key={step.n} ref={(node) => { steps.current[i] = node }} data-active={active === i} data-past={active > i}>
              <span className={s.stepN}>{step.n}</span>
              <div>
                <h3 className={s.stepT}>{step.t}</h3>
                <p className={s.stepD}>{step.d}</p>
              </div>
            </li>
          ))}
        </ol>
        <div className={s.stepBar} aria-hidden="true"><span style={{ transform: `scaleX(${(active + 1) / 3})` }} /></div>
      </div>
    </section>
  )
}
