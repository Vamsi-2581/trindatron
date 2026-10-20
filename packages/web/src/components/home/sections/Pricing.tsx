'use client'

import { useEffect, useState } from 'react'
import { Check } from 'lucide-react'
import { apiListPlanPrices } from '@/api/client'
import type { PlanPrice } from '@/types'
import { useLocale } from '../i18n'
import { rememberPlanIntent } from '../chat/funnel'
import s from '../home.module.css'

interface TeamPricing {
  seats: Record<'standard' | 'premium', { price: string; entitlement: string }>
  min_seats: number
  max_seats: number
  currency: string
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'

/**
 * Plans only after the story. Every figure comes from the server catalogue
 * (GET /billing/plans and GET /teams/pricing); nothing is priced here. If the
 * catalogue cannot be reached, the page says so instead of guessing.
 */
export function Pricing({ onChoose }: { onChoose: (plan: string) => void }) {
  const { t } = useLocale()
  const p = t.pricing
  const [prices, setPrices] = useState<Record<string, PlanPrice> | null>(null)
  const [team, setTeam] = useState<TeamPricing | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    apiListPlanPrices()
      .then((list) => setPrices(Object.fromEntries(list.map((x) => [x.plan, x]))))
      .catch(() => setFailed(true))
    fetch(`${API_BASE}/teams/pricing`, { headers: { 'X-TRINDATRON-Client': 'web' } })
      .then((r) => (r.ok ? r.json() : null))
      .then((d: TeamPricing | null) => setTeam(d))
      .catch(() => setTeam(null))
  }, [])

  const money = (amount: string | undefined, currency: string | undefined) => {
    const value = Number(amount)
    if (!amount || !currency || !Number.isFinite(value)) return null
    try {
      return new Intl.NumberFormat(t.meta.locale, { style: 'currency', currency, minimumFractionDigits: 2 }).format(value)
    } catch {
      return `${currency} ${value.toFixed(2)}`
    }
  }

  const priceFor = (plan: 'start' | 'pro' | 'max') => {
    const hit = prices?.[plan]
    return money(hit?.amount, hit?.currency)
  }

  const plans = [
    { key: 'free', price: p.free, per: '' },
    { key: 'start', price: priceFor('start'), per: p.perMonth },
    { key: 'pro', price: priceFor('pro'), per: p.perMonth, featured: true },
    { key: 'max', price: priceFor('max'), per: p.perMonth },
  ] as const

  return (
    <section id="pricing" className={s.pricing} data-stage-cover aria-labelledby="pricing-title">
      <header className={s.pricingHead}>
        <p className={s.kicker} data-reveal>{p.kicker}</p>
        <h2 id="pricing-title" className={s.h2} data-reveal>{p.title}</h2>
        <p className={s.lead} data-reveal>{p.sub}</p>
      </header>

      <ol className={s.plans}>
        {plans.map((plan) => {
          const copy = p.plans[plan.key]
          return (
            <li key={plan.key} className={s.plan} data-featured={'featured' in plan} data-reveal>
              {'featured' in plan && <span className={s.planFlag}>{p.popular}</span>}
              <p className={s.planName}>{copy.name}</p>
              <p className={s.planPrice}>
                {plan.price ? (
                  <>
                    <strong>{plan.price}</strong>
                    {plan.per && <span>{plan.per}</span>}
                  </>
                ) : (
                  <span className={s.planPending}>{failed ? p.unavailable : p.loading}</span>
                )}
              </p>
              <p className={s.planLine}>{copy.line}</p>
              <ul className={s.planPoints}>
                {copy.points.map((pt) => <li key={pt}><Check size={15} aria-hidden="true" /> {pt}</li>)}
              </ul>
              <button type="button" className={'featured' in plan ? s.btnDark : s.btnOutline} onClick={() => { rememberPlanIntent(plan.key); onChoose(plan.key) }}>
                {copy.cta}
              </button>
            </li>
          )
        })}
      </ol>

      <div className={s.team} data-reveal>
        <div className={s.teamIntro}>
          <p className={s.planName}>{p.team.name}</p>
          <p className={s.planLine}>{p.team.line}</p>
          <ul className={s.planPoints}>
            {p.team.points.map((pt) => <li key={pt}><Check size={15} aria-hidden="true" /> {pt}</li>)}
          </ul>
        </div>
        <dl className={s.seats}>
          {(['standard', 'premium'] as const).map((seat) => (
            <div key={seat}>
              <dt>{seat === 'standard' ? p.team.standard : p.team.premium} <span>{seat === 'standard' ? p.team.standardNote : p.team.premiumNote}</span></dt>
              <dd>
                {team ? (
                  <><strong>{money(team.seats[seat].price, team.currency)}</strong><span>{p.perSeat}</span></>
                ) : (
                  <span className={s.planPending}>{p.loading}</span>
                )}
              </dd>
            </div>
          ))}
        </dl>
        <button type="button" className={s.btnOutline} onClick={() => { rememberPlanIntent('team'); onChoose('team') }}>{p.team.cta}</button>
      </div>
    </section>
  )
}
