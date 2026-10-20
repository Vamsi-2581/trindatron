'use client'

import { useEffect, useReducer } from 'react'
import { ArrowUpRight, Check, FileText } from 'lucide-react'
import { BrandSymbol } from '@/components/brand/master'
import { useCopy } from './locale-provider'

/**
 * The product, working.
 *
 * A visitor deciding whether this is worth ten minutes wants to see the thing
 * run, not read about how it runs. So the hero carries a small, honest
 * reconstruction of a real task: the request goes in, the steps happen in
 * order, a result comes out.
 *
 * The steps are the ones the product actually performs and the copy for them
 * lives in the dictionary, so it is the same claim in three languages. It is
 * a reconstruction, not a live session — which is why it never shows numbers,
 * a fake latency, or anything a visitor could mistake for their own data.
 *
 * Reduced motion gets the finished state immediately: the point is the shape
 * of the work, and that is fully legible standing still.
 */

const STEP_MS = 1150
const HOLD_MS = 3600

type State = { step: number; done: boolean }
type Action = { type: 'advance'; total: number } | { type: 'restart' } | { type: 'complete'; total: number }

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case 'advance':
      return state.step >= action.total
        ? { step: action.total, done: true }
        : { step: state.step + 1, done: false }
    case 'complete':
      return { step: action.total, done: true }
    case 'restart':
      return { step: 0, done: false }
  }
}

export function WorkSurface() {
  const copy = useCopy()
  const steps = copy.inAction.steps
  const [state, dispatch] = useReducer(reducer, { step: 0, done: false })

  useEffect(() => {
    const reduced =
      typeof window !== 'undefined' &&
      window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    if (reduced) {
      dispatch({ type: 'complete', total: steps.length })
      return
    }
    const delay = state.done ? HOLD_MS : STEP_MS
    const id = window.setTimeout(
      () => dispatch(state.done ? { type: 'restart' } : { type: 'advance', total: steps.length }),
      delay,
    )
    return () => window.clearTimeout(id)
  }, [state, steps.length])

  return (
    <div className="relative">
      {/* Light source behind the surface. One bloom, not a gradient stack. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -inset-10 rounded-[3rem] bg-[radial-gradient(60%_50%_at_60%_30%,rgba(0,194,212,0.13),transparent_70%)] blur-2xl"
      />

      <div className="relative overflow-hidden rounded-[1.5rem] border border-border bg-surface-1/90 shadow-[0_30px_80px_-32px_rgba(0,0,0,0.9)] backdrop-blur-xl">
        {/* Chrome top edge — the one place the brand material appears as a rule. */}
        <div aria-hidden="true" className="h-px w-full bg-[var(--chrome-ramp)] opacity-70" />

        <div className="flex items-center gap-2.5 border-b border-border-subtle px-5 py-3.5">
          <BrandSymbol size={18} />
          <span className="text-[11px] font-semibold uppercase tracking-[0.16em] text-text-muted">
            {copy.intelligences.coreLabel}
          </span>
          <span className="ml-auto flex items-center gap-1.5 text-[11px] text-text-muted">
            <span
              aria-hidden="true"
              className={`h-1.5 w-1.5 rounded-full ${state.done ? 'bg-success' : 'bg-accent'}`}
            />
            {state.done ? copy.inAction.resultLabel : copy.work.eyebrow}
          </span>
        </div>

        {/* The request */}
        <div className="px-5 pt-5">
          <p className="ml-auto max-w-[92%] rounded-2xl rounded-br-md border border-accent/25 bg-accent/10 px-4 py-3 text-[0.9rem] leading-relaxed text-text-primary">
            {copy.inAction.request}
          </p>
        </div>

        {/* The work */}
        <ol className="space-y-0 px-5 py-4">
          {steps.map((s, i) => {
            const active = i === state.step && !state.done
            const complete = i < state.step || state.done
            return (
              <li
                key={s.label}
                className={[
                  'flex items-start gap-3 py-2 transition-opacity duration-500',
                  complete || active ? 'opacity-100' : 'opacity-25',
                ].join(' ')}
              >
                <span
                  aria-hidden="true"
                  className={[
                    'mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border transition-colors duration-300',
                    complete
                      ? 'border-accent/50 bg-accent/15 text-accent-light'
                      : active
                        ? 'border-accent/60 text-accent-light'
                        : 'border-border text-transparent',
                  ].join(' ')}
                >
                  {complete ? (
                    <Check className="h-3 w-3" />
                  ) : (
                    <span className={active ? 'h-1.5 w-1.5 rounded-full bg-accent' : ''} />
                  )}
                </span>
                <span className="min-w-0">
                  <span className="block text-[0.875rem] font-medium text-text-primary">
                    {s.label}
                  </span>
                  <span className="block text-[0.8rem] leading-snug text-text-muted">
                    {s.detail}
                  </span>
                </span>
              </li>
            )
          })}
        </ol>

        {/* The result */}
        <div
          className={[
            'mx-5 mb-5 flex items-center gap-3 rounded-xl border px-4 py-3 transition-all duration-500',
            state.done
              ? 'border-accent/35 bg-accent/[0.07] opacity-100'
              : 'border-border bg-surface-2/50 opacity-35',
          ].join(' ')}
        >
          <FileText className="h-4 w-4 shrink-0 text-accent-light" aria-hidden="true" />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-[0.85rem] font-medium text-text-primary">
              {copy.inAction.resultBody}
            </span>
            <span className="block text-[0.75rem] text-text-muted">{copy.inAction.resultMeta}</span>
          </span>
          <ArrowUpRight className="h-4 w-4 shrink-0 text-text-muted" aria-hidden="true" />
        </div>
      </div>
    </div>
  )
}
