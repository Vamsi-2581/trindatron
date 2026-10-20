'use client'

import { useLocale } from '../i18n'
import s from '../home.module.css'

export const CAPS = ['web', 'minds', 'files', 'voice', 'memory', 'approval'] as const
export type CapId = (typeof CAPS)[number]

/** A giant typographic index of what works today; every line opens its own scene below. */
export function Today() {
  const { t } = useLocale()
  return (
    <section id="today" className={s.today} data-chapter="today" aria-labelledby="today-title">
      <p className={s.kicker} data-reveal>{t.today.kicker}</p>
      <h2 id="today-title" className={s.h2} data-reveal>{t.today.title}</h2>
      <ol className={s.index}>
        {CAPS.map((id) => (
          <li key={id} data-reveal>
            <a href={`#cap-${id}`}>
              <span className={s.indexN}>{t.caps[id].n}</span>
              <span className={s.indexT}>{t.today.rows[id]}</span>
              <span className={s.indexPlan}>{t.caps[id].plan}</span>
            </a>
          </li>
        ))}
      </ol>
    </section>
  )
}
