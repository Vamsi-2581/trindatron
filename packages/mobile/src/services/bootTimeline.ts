import { Platform } from 'react-native'

/**
 * Startup, measured on the device instead of guessed.
 *
 *   T0 launch             native process start (React Native startup timing)
 *   T1 native splash      drawn by the system at launch; no later JS hook exists, so it equals T0
 *   T2 JS bundle          the bundle began executing
 *   T3 restore started    the saved session is being read
 *   T4 restore done       signed in or out is decided
 *   T5 profile            the account profile is known (network or cache)
 *   T6 capabilities       what this account can do is known
 *   T7 Home render        Home drew its first frame
 *   T8 composer usable    Home is on screen with nothing covering it (the opening has finished)
 *
 * Every mark uses one monotonic clock (performance.now, which React Native's own
 * startup marks also use). Development builds report the timeline once to the
 * Core's development-only diagnostics route; production builds never send it.
 */

export type BootMark = 'restoreStart' | 'restoreDone' | 'profile' | 'capabilities' | 'homeRender' | 'introDone' | 'composerUsable'

const marks: Partial<Record<BootMark, number>> = {}
const durations: Record<string, number> = {}
let reported = false
let reporter: ((payload: BootTimeline) => Promise<void>) | null = null

function clock(): number {
  return typeof performance !== 'undefined' && typeof performance.now === 'function' ? performance.now() : Date.now()
}

export function markBoot(mark: BootMark): void {
  if (marks[mark] === undefined) marks[mark] = clock()
  if ((mark === 'homeRender' || mark === 'introDone') && marks.homeRender !== undefined && marks.introDone !== undefined) {
    if (marks.composerUsable === undefined) marks.composerUsable = Math.max(marks.homeRender, marks.introDone)
    report()
  }
}

/** A step's own length (e.g. how long the secure store read took), recorded once. */
export function recordBootDuration(name: string, milliseconds: number): void {
  if (durations[name] === undefined) durations[name] = Math.round(milliseconds)
}

/** Time a promise as a boot step without changing its result. */
export async function timeBootStep<T>(name: string, work: Promise<T>): Promise<T> {
  const started = clock()
  try {
    return await work
  } finally {
    recordBootDuration(name, clock() - started)
  }
}

export interface BootTimeline {
  platform: string
  origin: 'native' | 'bundle' | 'unknown'
  marks: Record<string, number | null>
  durations: Record<string, number>
}

function nativeLaunch(): number | null {
  const startup = (globalThis.performance as unknown as { rnStartupTiming?: { startTime?: number } } | undefined)?.rnStartupTiming
  return typeof startup?.startTime === 'number' && startup.startTime > 0 ? startup.startTime : null
}

export function bootTimeline(): BootTimeline {
  const launch = nativeLaunch()
  const bundle = (globalThis as { __BUNDLE_START_TIME__?: number }).__BUNDLE_START_TIME__
  const base = launch ?? (typeof bundle === 'number' ? bundle : null)
  const relative = (value: number | undefined) => (value === undefined || base === null ? null : Math.round(value - base))
  return {
    platform: Platform.OS,
    origin: launch !== null ? 'native' : typeof bundle === 'number' ? 'bundle' : 'unknown',
    marks: {
      T0_launch: base === null ? null : 0,
      T1_nativeSplash: base === null ? null : 0,
      T2_jsBundle: relative(typeof bundle === 'number' ? bundle : undefined),
      T3_restoreStart: relative(marks.restoreStart),
      T4_restoreDone: relative(marks.restoreDone),
      T5_profile: relative(marks.profile),
      T6_capabilities: relative(marks.capabilities),
      T7_homeRender: relative(marks.homeRender),
      T8_composerUsable: relative(marks.composerUsable),
      introDone: relative(marks.introDone),
    },
    durations: { ...durations },
  }
}

/** Where a development build sends its timeline. Set once at startup. */
export function setBootReporter(send: (payload: BootTimeline) => Promise<void>): void {
  reporter = send
  report()
}

function report(): void {
  if (reported || !reporter || marks.composerUsable === undefined) return
  reported = true
  // Late marks (capabilities after Home) still make it: send after a short settle.
  setTimeout(() => void reporter?.(bootTimeline()).catch(() => undefined), 1500)
}

/** Tests only. */
export function resetBootTimeline(): void {
  for (const key of Object.keys(marks)) delete marks[key as BootMark]
  for (const key of Object.keys(durations)) delete durations[key]
  reported = false
  reporter = null
}
