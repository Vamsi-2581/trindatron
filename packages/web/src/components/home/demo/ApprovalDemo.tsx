'use client'

import { useState } from 'react'
import { Globe, ShieldCheck } from 'lucide-react'
import { BrandSymbol } from '@/components/brand/master'
import { useLocale } from '../i18n'
import s from '../home.module.css'

/**
 * What the approval step looks like, and how it feels to answer it. Clearly
 * labelled as an illustration: it is the same card the product shows before a
 * click or a form on a real site, not a recording.
 */
export function ApprovalDemo() {
  const { t } = useLocale()
  const a = t.approvalDemo
  const [answer, setAnswer] = useState<'approved' | 'rejected' | null>(null)

  return (
    <div className={s.approvalDemo}>
      <p className={s.demoLabel}>{a.label}</p>
      <div className={s.approvalCard} data-answer={answer ?? 'waiting'}>
        <div className={s.approvalHead}>
          <BrandSymbol size={26} />
          <ShieldCheck size={16} aria-hidden="true" />
          <p className={s.approvalWants}>{a.wants}</p>
        </div>
        <p className={s.approvalAction}>{a.action}</p>
        <p className={s.approvalSite}><Globe size={13} aria-hidden="true" /> {a.site}</p>
        {answer ? (
          <div className={s.approvalResult}>
            <p className={s.approvalDone}>{answer === 'approved' ? a.approved : a.rejected}</p>
            <button type="button" className={s.textButton} onClick={() => setAnswer(null)}>{a.again}</button>
          </div>
        ) : (
          <div className={s.approvalButtons}>
            <button type="button" className={s.btnPrimary} onClick={() => setAnswer('approved')}>{a.approve}</button>
            <button type="button" className={s.btnGhost} onClick={() => setAnswer('rejected')}>{a.reject}</button>
          </div>
        )}
      </div>
      <p className={s.demoCaption}>{t.minds.illustration}</p>
    </div>
  )
}
