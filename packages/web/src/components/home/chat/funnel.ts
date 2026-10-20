/**
 * Conversion structure for the public experience, prepared for later.
 *
 * Every step of the visitor funnel is emitted as a typed event. Today nothing
 * consumes them: they go to a DOM CustomEvent (and to `window.dataLayer` if an
 * analytics tag is ever installed). Wiring analytics or a checkout later means
 * listening here, not editing the experience. No payment happens anywhere in
 * this module.
 */

export type FunnelEvent =
  | { name: 'first_message' }
  | { name: 'gate_shown' }
  | { name: 'signup_completed' }
  | { name: 'login_completed' }
  | { name: 'first_reply' }
  | { name: 'plan_intent'; plan: string }

declare global {
  interface Window {
    dataLayer?: unknown[]
  }
}

export function track(event: FunnelEvent): void {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent('trindatron:funnel', { detail: event }))
  window.dataLayer?.push({ event: `trindatron_${event.name}`, ...event })
}

const PLAN_INTENT_KEY = 'trindatron_plan_intent'
const PENDING_KEY = 'trindatron_pending_message'

/** The plan a visitor showed interest in, kept for the future checkout. */
export function rememberPlanIntent(plan: string): void {
  try {
    sessionStorage.setItem(PLAN_INTENT_KEY, plan)
  } catch {
    // Private mode: intent is a nicety, never a requirement.
  }
  track({ name: 'plan_intent', plan })
}

export function readPlanIntent(): string | null {
  try {
    return sessionStorage.getItem(PLAN_INTENT_KEY)
  } catch {
    return null
  }
}

/** The message waiting behind the sign-in gate survives a reload. */
export function savePending(text: string | null): void {
  try {
    if (text) sessionStorage.setItem(PENDING_KEY, text)
    else sessionStorage.removeItem(PENDING_KEY)
  } catch {
    // ignore
  }
}

export function readPending(): string | null {
  try {
    return sessionStorage.getItem(PENDING_KEY)
  } catch {
    return null
  }
}
