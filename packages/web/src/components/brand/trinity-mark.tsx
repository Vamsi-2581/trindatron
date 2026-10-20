import type { SVGProps } from 'react'

/**
 * Whether the official master artwork has been added to the repository.
 *
 * The real TRINDATRON mark is a chrome trefoil lit in cyan. That drawing
 * does not live here yet, and it must not be reconstructed from a
 * description or a screenshot — a redrawn logo is a different logo, and it
 * would propagate into the favicon, the social card and every deck before
 * anyone noticed. So the product ships a deliberate stand-in instead, and
 * this flag is the switch between the two.
 *
 * To adopt the real mark: drop the files into `public/brand/master/` with
 * the names listed in `public/brand/README.md`, and flip this to `true`.
 * The flag is manual on purpose — reading the filesystem at module scope
 * would make this module server-only, and it is imported by client
 * components (the marketing nav, the app sidebar). A one-line change in the
 * same commit that adds the art is the cheaper honesty.
 */
export const HAS_MASTER_ART = false

const MASTER = {
  mark: '/brand/master/trindatron-mark.svg',
  lockup: '/brand/master/trindatron-lockup.svg',
  icon: '/brand/master/trindatron-icon.svg',
} as const

/**
 * The TRINDATRON mark.
 *
 * Until the master artwork lands this renders the stand-in triad: three
 * nodes, one system. This component is the source of truth for that
 * stand-in geometry — the standalone files under `public/brand/` and the
 * generated social/app icons are all derived from these exact coordinates.
 * Change it here first, never there.
 *
 * Colour comes from `currentColor`, so the stand-in inherits whatever text
 * colour it sits in and works on any background without a second asset.
 * The master artwork carries its own colour and ignores `currentColor`,
 * which is correct: a real logo is not a glyph to be tinted.
 */
export function TrinityMark(props: SVGProps<SVGSVGElement>) {
  if (HAS_MASTER_ART) {
    const { className, style } = props
    // eslint-disable-next-line @next/next/no-img-element
    return <img src={MASTER.mark} alt="TRINDATRON" className={className} style={style} />
  }
  return (
    <svg viewBox="0 0 32 28" fill="none" xmlns="http://www.w3.org/2000/svg" {...props}>
      <circle cx="16" cy="4"  r="3.5" fill="currentColor" opacity="1" />
      <circle cx="4.5" cy="24" r="3.5" fill="currentColor" opacity="0.6" />
      <circle cx="27.5" cy="24" r="3.5" fill="currentColor" opacity="0.35" />
    </svg>
  )
}

/**
 * The mark inside its accent container — the app/product icon form.
 *
 * Distinct geometry from `TrinityMark` on purpose: at favicon sizes the bare
 * triad loses its silhouette against a browser tab, so the official icon
 * tightens the triangle and sits it on a rounded accent square. Matches
 * `src/app/icon.svg` and `public/brand/trindatron-icon.svg` exactly.
 */
export function TrinityIcon(props: SVGProps<SVGSVGElement>) {
  if (HAS_MASTER_ART) {
    const { className, style } = props
    // eslint-disable-next-line @next/next/no-img-element
    return <img src={MASTER.icon} alt="TRINDATRON" className={className} style={style} />
  }
  return (
    <svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" {...props}>
      <rect width="32" height="32" rx="8" fill="#00c2d4" />
      <circle cx="16" cy="9"  r="3" fill="#ffffff" opacity="1" />
      <circle cx="9"  cy="22" r="3" fill="#ffffff" opacity="0.7" />
      <circle cx="23" cy="22" r="3" fill="#ffffff" opacity="0.5" />
    </svg>
  )
}

/**
 * The wordmark on its own. Inter 600 with the official 0.15em tracking —
 * live text rather than outlines, so it stays selectable, searchable and
 * pixel-perfect at every density.
 */
export function TrinityWordmark({ className = '' }: { className?: string }) {
  return (
    <span className={`font-semibold tracking-[0.15em] ${className}`}>TRINDATRON</span>
  )
}

const LOGO_SIZES = {
  sm: { mark: 'w-4 h-3.5',  text: 'text-sm' },
  md: { mark: 'w-5 h-4',    text: 'text-base' },
  lg: { mark: 'w-7 h-6',    text: 'text-xl' },
  xl: { mark: 'w-9 h-8',    text: 'text-2xl' },
} as const

/**
 * Horizontal lockup: mark + wordmark, the primary logo form.
 *
 * `tone` picks the mark colour: `accent` for normal use, `mono` to inherit
 * the surrounding text colour (footers, inverted surfaces, print).
 */
export function TrinityLogo({
  size = 'md',
  tone = 'accent',
  className = '',
}: {
  size?: keyof typeof LOGO_SIZES
  tone?: 'accent' | 'mono'
  className?: string
}) {
  const s = LOGO_SIZES[size]

  // The master lockup already contains the wordmark, drawn and spaced by
  // whoever made it. Pairing it with our own typeset wordmark would print
  // the name twice and second-guess the artwork's own spacing.
  if (HAS_MASTER_ART) {
    // eslint-disable-next-line @next/next/no-img-element
    return <img src={MASTER.lockup} alt="TRINDATRON" className={`${s.mark} w-auto ${className}`} />
  }

  return (
    <span className={`inline-flex items-center gap-2.5 ${className}`}>
      <TrinityMark className={`${s.mark} ${tone === 'accent' ? 'text-accent' : ''}`} />
      <TrinityWordmark className={`text-text-primary ${s.text}`} />
    </span>
  )
}
