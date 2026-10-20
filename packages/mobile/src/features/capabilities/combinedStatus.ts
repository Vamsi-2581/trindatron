import { statusOf, type CapabilityState, type CapabilityStatus } from './capabilities'

/** Most usable first: what a screen says is decided by the best reason among the capabilities it needs. */
const PRIORITY: CapabilityStatus[] = ['available', 'unavailable', 'plan', 'not_integrated']

/**
 * The status of a feature that any one of several capabilities can serve (Computer Use: the browser or the
 * virtual computer). If one works, it works. Otherwise "down right now" beats "not in your plan": when the plan
 * includes one of them, selling an upgrade would change nothing. Only if every one is outside the plan is the
 * plan the answer.
 */
export function combinedStatus(...states: (CapabilityState | undefined)[]): CapabilityStatus {
  const statuses = new Set(states.map(statusOf))
  return PRIORITY.find((status) => statuses.has(status)) ?? 'not_integrated'
}
