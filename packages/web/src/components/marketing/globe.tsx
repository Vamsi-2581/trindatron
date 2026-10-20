'use client'

import { useEffect, useRef, useState, useId } from 'react'
import { Check } from 'lucide-react'
import type { Locale } from '@/i18n'
import { LOCALE_LABELS, LOCALE_ORDER } from './copy'
import { useCopy, useLocale } from './locale-provider'

/**
 * The globe.
 *
 * A language control that looks like the product rather than like a form
 * field. It is drawn, not loaded: latitude rings, meridian arcs and a
 * specular sweep, all SVG and CSS. No WebGL, no globe library, no texture —
 * the entire control costs a few hundred bytes of markup and one CSS
 * animation, which is the right budget for a header ornament.
 *
 * It is a real sphere rather than a flat circle with lines: the meridians are
 * ellipses whose horizontal radius steps down toward the edge, which is what
 * a wireframe sphere actually projects to, and the sweep crosses them so the
 * form reads as round even while still.
 */

const MERIDIAN_RX = [30, 24, 15.5, 0.6] // ellipse radii from the equator outward
const LATITUDE = [
  { cy: 0, ry: 0, rx: 30 },
  { cy: -11.2, ry: 4.2, rx: 27.8 },
  { cy: 11.2, ry: 4.2, rx: 27.8 },
  { cy: -20.4, ry: 7, rx: 22 },
  { cy: 20.4, ry: 7, rx: 22 },
]

export function GlobeMark({ size = 22, spinning = true }: { size?: number; spinning?: boolean }) {
  const gid = useId().replace(/:/g, '')
  return (
    <svg
      viewBox="-34 -34 68 68"
      width={size}
      height={size}
      aria-hidden="true"
      className="shrink-0 overflow-visible"
    >
      <defs>
        <radialGradient id={`${gid}-body`} cx="34%" cy="28%" r="78%">
          <stop offset="0%" stopColor="rgb(var(--accent-light-ch) / 0.30)" />
          <stop offset="52%" stopColor="rgb(var(--accent-ch) / 0.12)" />
          <stop offset="100%" stopColor="rgb(var(--accent-ch) / 0.02)" />
        </radialGradient>
        <linearGradient id={`${gid}-sweep`} x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="rgb(var(--accent-light-ch) / 0)" />
          <stop offset="50%" stopColor="rgb(var(--accent-light-ch) / 0.55)" />
          <stop offset="100%" stopColor="rgb(var(--accent-light-ch) / 0)" />
        </linearGradient>
        <clipPath id={`${gid}-clip`}>
          <circle cx="0" cy="0" r="30" />
        </clipPath>
      </defs>

      <circle cx="0" cy="0" r="30" fill={`url(#${gid}-body)`} />

      <g
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        vectorEffect="non-scaling-stroke"
        opacity="0.75"
      >
        {LATITUDE.map((l, i) =>
          l.ry === 0 ? (
            <line key={i} x1={-l.rx} y1="0" x2={l.rx} y2="0" />
          ) : (
            <ellipse key={i} cx="0" cy={l.cy} rx={l.rx} ry={l.ry} />
          ),
        )}
        {MERIDIAN_RX.map((rx, i) => (
          <ellipse key={i} cx="0" cy="0" rx={rx} ry="30" />
        ))}
      </g>

      {/* The sweep is what makes a still drawing read as a rotating body. */}
      <g clipPath={`url(#${gid}-clip)`}>
        <rect
          x="-90"
          y="-32"
          width="120"
          height="64"
          fill={`url(#${gid}-sweep)`}
          className={spinning ? 'globe-lon' : undefined}
          style={{ transform: 'translateX(0)' }}
        />
      </g>

      <circle
        cx="0"
        cy="0"
        r="30"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        vectorEffect="non-scaling-stroke"
        opacity="0.95"
      />
    </svg>
  )
}

/**
 * The globe as a language switcher.
 *
 * Opens a small list of the languages this page is actually published in.
 * Selecting one applies it immediately and remembers it — the same storage
 * the product itself reads, so the choice follows the visitor inside.
 */
export function LanguageGlobe({ className = '' }: { className?: string }) {
  const { locale, setLocale } = useLocale()
  const copy = useCopy()
  const [open, setOpen] = useState(false)
  const [pulse, setPulse] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const listId = useId()

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const choose = (next: Locale) => {
    setLocale(next)
    setOpen(false)
    // The globe acknowledges the change — a half-second spin-up, so the
    // control confirms it did something without a toast.
    setPulse(true)
    window.setTimeout(() => setPulse(false), 700)
  }

  return (
    <div ref={wrapRef} className={`relative ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-label={copy.nav.language}
        className={[
          'group inline-flex items-center gap-2 rounded-full border px-2.5 py-1.5',
          'text-xs font-medium transition-colors duration-200',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg',
          open
            ? 'border-accent/45 bg-accent/10 text-accent-light'
            : 'border-border text-text-secondary hover:border-accent/35 hover:text-text-primary',
        ].join(' ')}
      >
        <span className={pulse ? 'text-accent-light' : 'text-accent-light/85'}>
          <GlobeMark size={17} spinning={pulse || open} />
        </span>
        <span className="tabular-nums tracking-wide">{LOCALE_LABELS[locale].code}</span>
      </button>

      {open && (
        <div
          id={listId}
          role="listbox"
          aria-label={copy.nav.languageHint}
          className={[
            'absolute right-0 top-[calc(100%+0.5rem)] z-50 w-52 overflow-hidden',
            'rounded-2xl border border-border bg-surface-1 shadow-lg',
          ].join(' ')}
        >
          <p className="border-b border-border-subtle px-3.5 py-2.5 text-[10px] font-semibold uppercase tracking-[0.16em] text-text-muted">
            {copy.nav.languageHint}
          </p>
          {LOCALE_ORDER.map((l) => {
            const active = l === locale
            return (
              <button
                key={l}
                type="button"
                role="option"
                aria-selected={active}
                onClick={() => choose(l)}
                className={[
                  'flex w-full items-center justify-between gap-3 px-3.5 py-2.5 text-left text-sm',
                  'transition-colors focus-visible:outline-none focus-visible:bg-surface-3',
                  active ? 'text-accent-light' : 'text-text-secondary hover:bg-surface-2 hover:text-text-primary',
                ].join(' ')}
              >
                <span className="flex items-baseline gap-2">
                  <span className="font-medium">{LOCALE_LABELS[l].name}</span>
                  <span className="text-[10px] uppercase tracking-wider text-text-muted">
                    {LOCALE_LABELS[l].code}
                  </span>
                </span>
                {active && <Check className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
