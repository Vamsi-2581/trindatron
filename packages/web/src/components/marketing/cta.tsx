'use client'

import Link from 'next/link'
import type { ReactNode } from 'react'
import { track, type MarketingEvent } from './analytics'

/**
 * Every conversion path on the page funnels through here.
 *
 * `SIGNUP_HREF` points at the existing auth screen with the register tab
 * preselected — no second auth system, no duplicated form. If the signup
 * route ever moves, this constant is the only thing that changes.
 */
export const SIGNUP_HREF = '/auth?signup=1'
export const LOGIN_HREF = '/auth'

type Variant = 'primary' | 'secondary' | 'quiet' | 'onPaper'
type Size = 'md' | 'lg'

const variants: Record<Variant, string> = {
  primary:
    'bg-accent text-text-inverse shadow-[0_8px_28px_-10px_rgba(0,194,212,0.85)] ' +
    'hover:bg-accent-hover hover:shadow-[0_10px_34px_-10px_rgba(0,194,212,1)] ' +
    'active:translate-y-px',
  secondary:
    'border border-border bg-surface-2/80 text-text-primary backdrop-blur-sm ' +
    'hover:border-accent/40 hover:bg-surface-3 active:translate-y-px',
  quiet:
    'text-text-secondary hover:text-text-primary',
  // For light sections. This exists as a variant rather than a className
  // override because overriding `text-text-primary` with another text colour
  // in the same class string is a coin flip — both utilities are present and
  // the stylesheet order decides, not the string order. The pricing cards'
  // secondary buttons were coming out near-white on white that way.
  onPaper:
    'border border-[color:var(--paper-edge)] bg-transparent text-[color:var(--ink)] ' +
    'hover:border-[color:var(--ink-3)] hover:bg-[color:var(--paper-2)] active:translate-y-px',
}

const sizes: Record<Size, string> = {
  md: 'h-10 px-4 text-sm rounded-xl',
  lg: 'h-12 px-6 text-[0.95rem] rounded-xl',
}

export function CTA({
  href,
  event,
  eventProps,
  variant = 'primary',
  size = 'md',
  children,
  className = '',
}: {
  href: string
  event?: MarketingEvent
  eventProps?: Record<string, string>
  variant?: Variant
  size?: Size
  children: ReactNode
  className?: string
}) {
  const external = href.startsWith('#')

  const classes = [
    'inline-flex select-none items-center justify-center gap-2 font-medium',
    'transition-all duration-150',
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg',
    variants[variant],
    sizes[size],
    className,
  ].join(' ')

  const onClick = () => event && track(event, eventProps)

  // In-page anchors stay plain <a> so the browser handles smooth scrolling
  // and history exactly as it would anywhere else.
  if (external) {
    return (
      <a href={href} onClick={onClick} className={classes}>
        {children}
      </a>
    )
  }

  return (
    <Link href={href} onClick={onClick} className={classes} prefetch={false}>
      {children}
    </Link>
  )
}
