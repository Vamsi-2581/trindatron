import type { ReactNode } from 'react'

/**
 * Layout primitives for the public site.
 *
 * The page alternates between two surfaces — graphite and paper — and the
 * single most common way that goes wrong is a component hard-coding
 * `text-text-secondary` and then being dropped onto paper, where it is
 * invisible. So sections declare their surface once and children use the
 * relative classes `t-1` / `t-2` / `t-3` (primary, secondary, quiet), which
 * resolve against whichever surface they landed on. See globals.css.
 */

export function Container({
  children,
  className = '',
}: {
  children: ReactNode
  className?: string
}) {
  return <div className={`mx-auto w-full max-w-6xl px-5 sm:px-8 ${className}`}>{children}</div>
}

export function Section({
  id,
  children,
  className = '',
  surface = 'graphite',
  tight = false,
}: {
  id?: string
  children: ReactNode
  className?: string
  surface?: 'graphite' | 'paper'
  tight?: boolean
}) {
  return (
    <section
      id={id}
      className={[
        surface,
        'scroll-mt-24',
        tight ? 'py-16 sm:py-20' : 'py-24 sm:py-32 lg:py-36',
        className,
      ].join(' ')}
    >
      {children}
    </section>
  )
}

/** The join between two surfaces. Without it they read as two stacked pages. */
export function Seam({ to }: { to: 'paper' | 'graphite' }) {
  return (
    <div
      aria-hidden="true"
      className={`h-16 sm:h-24 ${to === 'paper' ? 'seam-to-paper' : 'seam-to-graphite'}`}
    />
  )
}

export function Eyebrow({ children }: { children: ReactNode }) {
  return (
    <p className="mb-5 flex items-center gap-2.5 text-[11px] font-semibold uppercase tracking-[0.2em] text-accent">
      <span aria-hidden="true" className="h-px w-6 bg-accent/60" />
      {children}
    </p>
  )
}

export function SectionHeading({
  eyebrow,
  title,
  body,
  align = 'left',
  className = '',
}: {
  eyebrow?: ReactNode
  title: ReactNode
  body?: ReactNode
  align?: 'left' | 'center'
  className?: string
}) {
  return (
    <div
      className={[
        'flex max-w-3xl flex-col',
        align === 'center' ? 'mx-auto items-center text-center' : 'text-left',
        className,
      ].join(' ')}
    >
      {eyebrow && <Eyebrow>{eyebrow}</Eyebrow>}
      <h2 className="t-1 text-balance text-[2rem] font-semibold leading-[1.1] tracking-[-0.025em] sm:text-[2.6rem] lg:text-[3rem]">
        {title}
      </h2>
      {body && (
        <p className="t-2 mt-6 max-w-2xl text-pretty text-[1.0625rem] leading-[1.65] sm:text-lg">
          {body}
        </p>
      )}
    </div>
  )
}

/** Hairline that fades at both ends, tinted by whichever surface holds it. */
export function Rule({ className = '' }: { className?: string }) {
  return <div aria-hidden="true" className={`hairline h-px w-full opacity-70 ${className}`} />
}

/**
 * A panel. `surface` says which ground it sits ON, so it can pick a contrast
 * that works there rather than assuming dark.
 */
export function Panel({
  children,
  className = '',
  on = 'graphite',
  interactive = false,
}: {
  children: ReactNode
  className?: string
  on?: 'graphite' | 'paper'
  interactive?: boolean
}) {
  const base =
    on === 'paper'
      ? 'border-[color:var(--paper-edge)] bg-white/70'
      : 'border-border bg-surface-1/80'
  const hover = interactive
    ? on === 'paper'
      ? 'transition-all duration-200 hover:border-accent/45 hover:bg-white'
      : 'transition-all duration-200 hover:border-accent/35 hover:bg-surface-2/80'
    : ''
  return <div className={`rounded-2xl border ${base} ${hover} ${className}`}>{children}</div>
}

/**
 * Marks what is working today against what is coming.
 *
 * Both states are written as a positive: one says the capability is running,
 * the other says it is on the way. Neither tells a visitor what the product
 * lacks — but the distinction is real and this is what keeps the page from
 * quietly promising the second as the first.
 */
export function StatusPill({
  status,
  live,
  next,
}: {
  status: 'live' | 'next'
  live: string
  next: string
}) {
  const isLive = status === 'live'
  return (
    <span
      className={[
        'inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1',
        'text-[10px] font-semibold uppercase tracking-[0.12em]',
        isLive
          ? 'border-accent/30 bg-accent/10 text-accent-light'
          : 'border-border bg-surface-2 text-text-secondary',
      ].join(' ')}
    >
      <span
        aria-hidden="true"
        className={`h-1.5 w-1.5 rounded-full ${isLive ? 'bg-accent' : 'bg-text-muted'}`}
      />
      {isLive ? live : next}
    </span>
  )
}
