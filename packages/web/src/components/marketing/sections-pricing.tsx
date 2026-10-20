'use client'

import { Check, Minus } from 'lucide-react'
import { BrandLockup } from '@/components/brand/master'
import { Container, Section, SectionHeading } from './primitives'
import { CTA, SIGNUP_HREF, LOGIN_HREF } from './cta'
import { Reveal } from './reveal'
import { useCopy, useLocale } from './locale-provider'
import type { PlanCopy } from './copy'

/**
 * Pricing.
 *
 * The amounts mirror PLAN_PRICES in packages/core/src/billing/pricing.py and
 * are asserted against it in src/tests/marketing-claims.test.ts. They are
 * literals here rather than an API call on purpose: this page is prerendered
 * and must render its own prices without the backend being reachable.
 *
 * Two plans are singled out, for different reasons. START carries the visual
 * emphasis because it is the step that actually converts — the move off FREE.
 * PRO carries the badge because it is the one most people should end up on.
 * Giving both to the same card would waste one of them.
 *
 * FREE is deliberately thin. Its entitlement really is narrow —
 * supports_agent_mode, supports_trinity_code and supports_trinity_work are
 * all False, max_agents is 0, max_projects is 3, and the router caps it below
 * the standard quality band — so the card says so plainly instead of listing
 * everything the product can do and letting the reader assume FREE does it.
 */

type PlanId = 'free' | 'start' | 'pro' | 'max' | 'team'

const PLANS: { id: PlanId; amount: number | null; emphasis?: 'ring' | 'badge' }[] = [
  { id: 'free', amount: null },
  { id: 'start', amount: 9.9, emphasis: 'ring' },
  { id: 'pro', amount: 19.9, emphasis: 'badge' },
  { id: 'max', amount: 79.9 },
  { id: 'team', amount: 199.9 },
]

/**
 * Price display, localised, in the one currency the product charges.
 *
 * USD is the base and stays the base — Intl decides only how a US dollar
 * amount is written for the reader ("$9.90", "US$ 9,90", "9,90 US$"), never
 * what it converts to. When per-region currencies arrive, this function is
 * where they land, and the globe is already the control that sets the locale.
 */
function formatPrice(amount: number, bcp47: string): string {
  try {
    return new Intl.NumberFormat(bcp47, {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 2,
    }).format(amount)
  } catch {
    return `US$ ${amount.toFixed(2)}`
  }
}

function PlanCard({
  id,
  amount,
  emphasis,
  plan,
  bcp47,
}: {
  id: PlanId
  amount: number | null
  emphasis?: 'ring' | 'badge'
  plan: PlanCopy
  bcp47: string
}) {
  const copy = useCopy()

  return (
    <div
      className={[
        'relative flex h-full flex-col rounded-2xl border p-6',
        emphasis === 'ring'
          ? 'border-accent/45 bg-white shadow-[0_20px_50px_-30px_rgba(0,120,135,0.55)]'
          : 'border-[color:var(--paper-edge)] bg-white/70',
      ].join(' ')}
    >
      {emphasis === 'badge' && (
        <span className="absolute -top-2.5 left-6 rounded-full bg-[color:var(--ink)] px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-[color:var(--paper)]">
          {copy.pricing.recommended}
        </span>
      )}

      <p className="t-1 text-[0.8rem] font-semibold uppercase tracking-[0.16em]">{id}</p>
      <p className="t-3 mt-1 text-[0.85rem]">{plan.tagline}</p>

      <p className="t-1 mt-6 flex items-baseline gap-1.5">
        <span className="text-[2rem] font-semibold tracking-tight">
          {amount === null ? copy.pricing.free : formatPrice(amount, bcp47)}
        </span>
        {amount !== null && <span className="t-3 text-[0.85rem]">{copy.pricing.perMonth}</span>}
      </p>

      <p className="t-2 mt-4 rounded-lg border border-[color:var(--paper-edge)] bg-[color:var(--paper-2)]/60 px-3 py-2 text-[0.8rem]">
        {plan.volume}
      </p>

      <ul className="mt-5 flex-1 space-y-2.5">
        {plan.features.map((f) => (
          <li key={f.label} className="flex items-start gap-2.5 min-w-0">
            {f.included ? (
              <Check
                className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[color:var(--accent-hover)]"
                aria-hidden="true"
              />
            ) : (
              <Minus className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[color:var(--ink-3)]" aria-hidden="true" />
            )}
            <span
              className={`min-w-0 break-words text-[0.85rem] leading-snug ${f.included ? 't-2' : 't-3'}`}
            >
              {f.label}
            </span>
          </li>
        ))}
      </ul>

      <CTA
        href={SIGNUP_HREF}
        event="pricing_cta"
        eventProps={{ plan: id }}
        variant={emphasis === 'ring' ? 'primary' : 'onPaper'}
        size="md"
        className="mt-6 w-full"
      >
        {plan.cta}
      </CTA>
    </div>
  )
}

export function Pricing() {
  const copy = useCopy()
  const { locale } = useLocale()
  const bcp47 = copy.meta.htmlLang

  return (
    <Section id="pricing" surface="paper">
      <Container>
        <Reveal>
          <SectionHeading
            eyebrow={copy.pricing.eyebrow}
            title={copy.pricing.title}
            body={copy.pricing.body}
            align="center"
          />
        </Reveal>

        {/* Five across only from 1280px. At 1024 the columns are ~180px and a
            longer translation ("Sessões que renovam a cada 5 horas") pushes
            past the viewport — the grid is what has to give there, not the
            sentence. */}
        <div className="mt-14 grid gap-5 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
          {PLANS.map((p, i) => (
            <Reveal key={p.id} delay={i * 60} className="h-full">
              <PlanCard {...p} plan={copy.pricing.plans[p.id]} bcp47={bcp47} key={locale} />
            </Reveal>
          ))}
        </div>

        <p className="t-3 mx-auto mt-10 max-w-3xl text-center text-[0.875rem] leading-relaxed">
          {copy.pricing.windowNote} {copy.pricing.currencyNote}
        </p>
      </Container>
    </Section>
  )
}

/**
 * Closing.
 *
 * One statement and one action. "Sign in" is present because customers read
 * this page too, but it stays the quiet one — the footer's job is to convert
 * a stranger, not to route an existing account.
 */
export function Closing() {
  const copy = useCopy()

  return (
    <Section surface="graphite" className="relative overflow-hidden">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute left-1/2 top-1/2 h-[26rem] w-[52rem] -translate-x-1/2 -translate-y-1/2 rounded-full bg-[radial-gradient(ellipse_at_center,rgba(0,194,212,0.11),transparent_66%)] blur-3xl"
      />
      <Container className="relative">
        <Reveal className="mx-auto flex max-w-2xl flex-col items-center text-center">
          <h2 className="chrome-type text-balance text-[2.1rem] font-semibold leading-[1.08] tracking-[-0.03em] sm:text-[3rem]">
            {copy.closing.title}
          </h2>
          <p className="t-2 mt-6 max-w-xl text-pretty text-[1.0625rem] leading-relaxed">
            {copy.closing.body}
          </p>
          <div className="mt-9 flex flex-col gap-3 sm:flex-row">
            <CTA
              href={SIGNUP_HREF}
              event="signup_cta"
              eventProps={{ place: 'closing' }}
              size="lg"
              className="w-full sm:w-auto"
            >
              {copy.closing.primary}
            </CTA>
            <CTA
              href={LOGIN_HREF}
              event="login_click"
              variant="secondary"
              size="lg"
              className="w-full sm:w-auto"
            >
              {copy.closing.secondary}
            </CTA>
          </div>
        </Reveal>
      </Container>
    </Section>
  )
}

export function Footer() {
  const copy = useCopy()
  const year = new Date().getFullYear()

  const links = [
    { href: '#product', label: copy.nav.product },
    { href: '#intelligence', label: copy.nav.intelligence },
    { href: '#work', label: copy.nav.work },
    { href: '#agents', label: copy.nav.agents },
    { href: '#reach', label: copy.nav.reach },
    { href: '#pricing', label: copy.nav.pricing },
  ]

  return (
    <footer className="graphite border-t border-border">
      <Container className="py-14">
        <div className="flex flex-col gap-10 md:flex-row md:items-start md:justify-between">
          <div className="max-w-xs">
            <BrandLockup size="sm" className="text-text-primary" />
            <p className="t-3 mt-4 text-[0.875rem] leading-relaxed">{copy.footer.tagline}</p>
          </div>

          <nav aria-label={copy.footer.product} className="flex flex-wrap gap-x-7 gap-y-3">
            {links.map((l) => (
              <a
                key={l.href}
                href={l.href}
                className="text-[0.875rem] text-text-secondary transition-colors hover:text-text-primary"
              >
                {l.label}
              </a>
            ))}
          </nav>
        </div>

        <div className="mt-12 flex flex-col-reverse gap-4 border-t border-border-subtle pt-6 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-[0.8rem] text-text-muted">
            © {year} TRINDATRON. {copy.footer.rights}
          </p>
          <a
            href={LOGIN_HREF}
            className="text-[0.8rem] text-text-muted transition-colors hover:text-text-secondary"
          >
            {copy.footer.signIn}
          </a>
        </div>
      </Container>
    </footer>
  )
}
