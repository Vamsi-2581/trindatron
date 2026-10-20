import Image from 'next/image'

/**
 * The official TRINDATRON artwork.
 *
 * These render the real master — a 3D chrome trefoil lit in cyan — copied
 * byte for byte from the brand folder ("IMAGENS LOGO") into
 * `public/brand/master/source/`, never redrawn or cut out.
 * `public/brand/README.md` records where each file came from.
 *
 * Why this is separate from `trinity-mark.tsx`: that file holds a flat,
 * `currentColor` glyph the app uses at 14–20px inside chat bubbles and the
 * sidebar. The master is a photographic render. Shrunk to 14px and tinted it
 * would be mud, and it cannot inherit a text colour. So the app keeps its
 * glyph and the brand surfaces use the real thing — which is also the only
 * place a visitor judges the brand.
 *
 * A note on the wordmark: the master carries "TRINDATRON" in black, set on
 * the render's own light backdrop. On a dark page that word is invisible and
 * it cannot be recoloured without repainting the artwork. So the lockup used
 * on dark pairs the real symbol with the wordmark set in type, and the
 * complete original lockup is used at display size on light surfaces, where
 * it belongs — see `BrandLockupPlate`.
 */

/**
 * The official symbol, from the master the brand folder designates for the
 * hero ("IMAGENS LOGO/3. Modelo Vertical  Empilhado"), copied byte for byte.
 * The master is the full stacked lockup on a graphite backdrop; this shows its
 * symbol only, by framing (the wordmark below is cropped out, never edited).
 *
 * The backdrop (max channel ~25-46), the drop shadow (darker still) and the
 * holes inside the loops are cleared at paint time by MATTE, which derives
 * alpha from the pixels' own brightness; the chrome starts around 55. Colour is
 * left untouched. The symbol therefore belongs on dark surfaces.
 */
const SYMBOL = '/brand/master/source/trindatron-lockup-vertical.jpg'
const MASTER_W = 944
const MASTER_H = 1112
/** Where the symbol sits inside the master, in master pixels. */
const SYMBOL_BOX = { x: 214, y: 255, w: 560, h: 528 }
const MATTE_ID = 'trd-symbol-matte'
// alpha = 5.56 * (R + G + B) - 2.89: backdrop (sum <= 0.49) fully clear, chrome
// (sum >= 0.70) fully opaque. RGB rows are the identity.
const MATTE = '1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  5.56 5.56 5.56 0 -2.89'
const LOCKUP_LIGHT = '/brand/master/trindatron-lockup-light.webp'

export function BrandSymbol({
  size = 28,
  className = '',
  priority = false,
}: {
  /** Rendered height in px. Width follows the symbol's own proportion. */
  size?: number
  className?: string
  priority?: boolean
}) {
  // Scale the whole master so the symbol measures `size` tall, and frame it.
  const scale = size / SYMBOL_BOX.h
  const width = Math.round(SYMBOL_BOX.w * scale)
  const imgW = Math.round(MASTER_W * scale)
  const imgH = Math.round(MASTER_H * scale)
  return (
    <span
      aria-hidden="true"
      className={`relative inline-block shrink-0 overflow-hidden select-none ${className}`}
      style={{ width, height: size }}
    >
      <svg width="0" height="0" style={{ position: 'absolute' }} focusable="false">
        <filter id={MATTE_ID} colorInterpolationFilters="sRGB">
          <feColorMatrix type="matrix" values={MATTE} />
        </filter>
      </svg>
      <Image
        src={SYMBOL}
        alt=""
        width={imgW}
        height={imgH}
        priority={priority}
        style={{
          position: 'absolute',
          left: -SYMBOL_BOX.x * scale,
          top: -SYMBOL_BOX.y * scale,
          width: imgW,
          height: imgH,
          maxWidth: 'none',
          filter: `url(#${MATTE_ID})`,
        }}
      />
    </span>
  )
}

/**
 * Symbol + wordmark, horizontal. The primary logo form on dark surfaces.
 *
 * The wordmark is live text rather than an image: it stays crisp at every
 * density, remains selectable and searchable, and — the reason it has to be
 * type here — can take the colour the surface needs.
 */
export function BrandLockup({
  size = 'md',
  className = '',
  priority = false,
}: {
  size?: 'sm' | 'md' | 'lg'
  className?: string
  priority?: boolean
}) {
  const px = size === 'sm' ? 22 : size === 'lg' ? 38 : 28
  const text = size === 'sm' ? 'text-[0.92rem]' : size === 'lg' ? 'text-2xl' : 'text-[1.05rem]'
  return (
    <span className={`inline-flex items-center gap-2.5 ${className}`}>
      <BrandSymbol size={px} priority={priority} />
      <span className={`font-semibold tracking-[0.15em] ${text}`}>TRINDATRON</span>
    </span>
  )
}

/**
 * The complete original lockup, on the light plate it was rendered against.
 *
 * This is the artwork exactly as the designer delivered it — symbol, wordmark
 * and studio backdrop — which is why it only appears on a light surface. It
 * is the one place on the page the brand is shown at full size rather than
 * used as a mark.
 */
export function BrandLockupPlate({
  className = '',
  priority = false,
}: {
  className?: string
  priority?: boolean
}) {
  return (
    <Image
      src={LOCKUP_LIGHT}
      alt="TRINDATRON"
      width={864}
      height={790}
      priority={priority}
      sizes="(max-width: 640px) 70vw, 340px"
      className={`select-none ${className}`}
    />
  )
}
