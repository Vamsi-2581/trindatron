'use client'

/**
 * Conversion instrumentation — zero dependencies, zero network calls of its own.
 *
 * There is no analytics vendor in this project and this file does not add one.
 * It defines the *call sites* so that adding a collector later is a one-line
 * change here instead of a hunt through twelve components. Until then each
 * event does two cheap, local things:
 *
 *   1. pushes onto `window.dataLayer` if some tag manager happens to be present
 *   2. dispatches a DOM CustomEvent, so anything can subscribe without imports
 *
 * Never pass user content, credentials, emails or ids through here — the
 * payloads below are fixed, low-cardinality labels describing which control
 * was used, nothing about who used it or what they typed.
 */

export type MarketingEvent =
  | 'hero_cta'
  | 'pricing_cta'
  | 'signup_cta'
  | 'login_click'
  | 'capability_click'
  | 'agent_click'
  | 'video_play'

type Props = Record<string, string>

declare global {
  interface Window {
    dataLayer?: unknown[]
  }
}

export function track(event: MarketingEvent, props: Props = {}): void {
  if (typeof window === 'undefined') return
  try {
    window.dataLayer?.push({ event: `trindatron_${event}`, ...props })
    window.dispatchEvent(new CustomEvent('trindatron:track', { detail: { event, ...props } }))
  } catch {
    // Instrumentation must never break a click.
  }
}
