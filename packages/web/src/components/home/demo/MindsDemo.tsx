'use client'

import { useEffect, useRef, useState } from 'react'
import { useLocale } from '../i18n'
import s from '../home.module.css'

/**
 * How TRINDATRON routes a request, drawn rather than recorded: three everyday
 * asks, each lighting the kind of intelligence it gets. Labelled as an
 * illustration; no provider or model is named, because the visitor never
 * needs to know.
 */
export function MindsDemo() {
  const { t } = useLocale()
  const m = t.minds
  const [i, setI] = useState(0)
  const root = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    let timer = 0
    let visible = false
    const tick = () => { if (visible) setI((n) => (n + 1) % m.asks.length); timer = window.setTimeout(tick, 2600) }
    const io = new IntersectionObserver(([e]) => { visible = e.isIntersecting }, { threshold: 0.3 })
    if (root.current) io.observe(root.current)
    timer = window.setTimeout(tick, 2600)
    return () => { window.clearTimeout(timer); io.disconnect() }
  }, [m.asks.length])

  const ask = m.asks[i]
  return (
    <div ref={root} className={s.minds}>
      <p className={s.demoLabel}>{m.label}</p>
      <ul className={s.mindsAsks}>
        {m.asks.map((a, n) => (
          <li key={a.q}>
            <button type="button" aria-pressed={n === i} onClick={() => setI(n)}>{a.q}</button>
          </li>
        ))}
      </ul>
      <div className={s.mindsRoute} aria-hidden="true">
        <span className={s.mindsCore} />
        {m.tiers.map((tier, n) => (
          <span key={tier} className={s.mindsLine} data-on={n === ask.tier} style={{ ['--n' as string]: n }} />
        ))}
      </div>
      <ol className={s.mindsTiers}>
        {m.tiers.map((tier, n) => (
          <li key={tier} data-on={n === ask.tier}>
            <span className={s.mindsOrb} style={{ ['--n' as string]: n }} />
            {tier}
          </li>
        ))}
      </ol>
      <p className={s.demoCaption}>{m.illustration}</p>
    </div>
  )
}
