'use client'

import { useId } from 'react'
import { BrandSymbol } from '@/components/brand/master'
import { useCopy } from './locale-provider'

/**
 * The intelligence field.
 *
 * The one diagram on the page, and it deliberately names nobody. Listing the
 * providers behind the product would date the page the moment the catalogue
 * changes — and worse, it tells a visitor to have an opinion about something
 * the product exists to decide for them.
 *
 * So it draws the shape of the idea instead: a field of capable intelligences
 * on the outside, one core in the middle, work converging inward. The nodes
 * carry no labels, which is the honest picture — the set is meant to grow.
 *
 * Drawn in SVG with three CSS keyframes. No canvas, no WebGL, no physics.
 */

/** Nodes on two rings — unlabelled on purpose. */
const OUTER = [0, 40, 78, 120, 163, 205, 246, 288, 325]
const INNER = [22, 96, 158, 228, 300]

function polar(deg: number, r: number) {
  const rad = ((deg - 90) * Math.PI) / 180
  return { x: Math.cos(rad) * r, y: Math.sin(rad) * r }
}

export function IntelligenceField() {
  const copy = useCopy()
  const id = useId().replace(/:/g, '')

  return (
    <div className="relative mx-auto aspect-square w-full max-w-[30rem]">
      <svg
        viewBox="-150 -150 300 300"
        className="absolute inset-0 h-full w-full overflow-visible"
        role="img"
        aria-label={copy.intelligences.caption}
      >
        <defs>
          <radialGradient id={`${id}-halo`} cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="rgb(var(--accent-ch) / 0.20)" />
            <stop offset="70%" stopColor="rgb(var(--accent-ch) / 0.05)" />
            <stop offset="100%" stopColor="rgb(var(--accent-ch) / 0)" />
          </radialGradient>
          <linearGradient id={`${id}-spoke`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgb(var(--accent-ch) / 0)" />
            <stop offset="100%" stopColor="rgb(var(--accent-ch) / 0.45)" />
          </linearGradient>
        </defs>

        <circle cx="0" cy="0" r="145" fill={`url(#${id}-halo)`} />

        {/* Rings. Counter-rotating, so the field reads as a body rather than
            a wheel — two speeds in one direction would just look like lag. */}
        <g className="field-ring" stroke="var(--border)" fill="none" strokeWidth="1">
          <circle cx="0" cy="0" r="132" strokeDasharray="2 10" opacity="0.85" />
        </g>
        <g className="field-ring-slow" stroke="var(--border)" fill="none" strokeWidth="1">
          <circle cx="0" cy="0" r="88" strokeDasharray="2 8" opacity="0.7" />
        </g>
        <circle cx="0" cy="0" r="46" stroke="rgb(var(--accent-ch) / 0.28)" fill="none" strokeWidth="1" />

        {/* Spokes: work travelling inward from the field to the core. */}
        {OUTER.map((deg, i) => {
          const a = polar(deg, 132)
          const b = polar(deg, 50)
          return (
            <line
              key={`s${deg}`}
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
              stroke={`url(#${id}-spoke)`}
              strokeWidth="1"
              opacity="0.5"
              className="field-pulse"
              style={{ animationDelay: `${(i * 0.42).toFixed(2)}s` }}
            />
          )
        })}

        {/* The field itself. */}
        {OUTER.map((deg, i) => {
          const p = polar(deg, 132)
          return (
            <circle
              key={`o${deg}`}
              cx={p.x}
              cy={p.y}
              r={i % 3 === 0 ? 4.5 : 3.2}
              fill="rgb(var(--accent-light-ch) / 0.85)"
              className="field-core"
              style={{ animationDelay: `${(i * 0.31).toFixed(2)}s` }}
            />
          )
        })}
        {INNER.map((deg, i) => {
          const p = polar(deg, 88)
          return (
            <circle
              key={`i${deg}`}
              cx={p.x}
              cy={p.y}
              r="2.6"
              fill="rgb(var(--accent-ch) / 0.8)"
              className="field-core"
              style={{ animationDelay: `${(i * 0.5 + 0.2).toFixed(2)}s` }}
            />
          )
        })}
      </svg>

      {/* The core: the real mark, at the centre of its own field. */}
      <div className="absolute left-1/2 top-1/2 flex -translate-x-1/2 -translate-y-1/2 flex-col items-center">
        <span className="relative flex h-[6.5rem] w-[6.5rem] items-center justify-center rounded-full border border-accent/30 bg-bg/85 backdrop-blur-sm">
          <span
            aria-hidden="true"
            className="absolute inset-0 rounded-full bg-[radial-gradient(circle,rgba(0,194,212,0.18),transparent_70%)]"
          />
          <BrandSymbol size={62} className="relative" />
        </span>
      </div>

      <p className="absolute inset-x-0 -bottom-2 text-center text-[11px] font-medium uppercase tracking-[0.18em] text-text-muted">
        {copy.intelligences.fieldLabel}
      </p>
    </div>
  )
}
