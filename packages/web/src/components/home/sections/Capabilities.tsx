'use client'

import { Replay } from '../demo/Replay'
import { MindsDemo } from '../demo/MindsDemo'
import { VoiceDemo } from '../demo/VoiceDemo'
import { ApprovalDemo } from '../demo/ApprovalDemo'
import { useLocale } from '../i18n'
import { CAPS, type CapId } from './Today'
import s from '../home.module.css'

const DEMOS: Record<CapId, React.ReactNode> = {
  minds: <MindsDemo />,
  web: <Replay demo="web" />,
  memory: <Replay demo="memory" />,
  files: <Replay demo="files" />,
  voice: <VoiceDemo />,
  approval: <ApprovalDemo />,
}

/**
 * One scene per capability that works today. The stage re-forms behind each
 * (orbiting minds, a turning globe, a strand of memory, a page being read, a
 * waveform, a gate), the words sit on one side and the proof on the other.
 */
export function Capabilities() {
  const { t } = useLocale()
  return (
    <>
      {CAPS.map((id, i) => {
        const cap = t.caps[id]
        return (
          <section
            key={id}
            id={`cap-${id}`}
            className={`${s.cap} ${i % 2 ? s.capFlip : ''}`}
            data-chapter={id}
            aria-labelledby={`cap-${id}-title`}
          >
            <div className={s.capText}>
              <p className={s.capKicker} data-reveal><span>{cap.n}</span> {cap.kicker}</p>
              <h2 id={`cap-${id}-title`} className={s.capTitle} data-reveal>{cap.title}</h2>
              <p className={s.capBody} data-reveal>{cap.body}</p>
              <p className={s.capBenefit} data-reveal>{cap.benefit}</p>
              <p className={s.planTag} data-reveal>{cap.plan}</p>
            </div>
            <div className={s.capDemo} data-reveal>{DEMOS[id]}</div>
          </section>
        )
      })}
    </>
  )
}
