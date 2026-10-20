import { Platform } from 'react-native'

/**
 * TRINDATRON design tokens, read from packages/web/src/app/globals.css.
 *
 * The palette comes from the official master mark (chrome trefoil lit in
 * cyan). The old purple #7c3aed was a pre-logo placeholder the Web already
 * removed; it must not come back here.
 */

export type ColorScheme = 'dark' | 'light'

export interface Palette {
  bg: string
  surface1: string
  surface2: string
  surface3: string
  surface4: string
  /** A layer raised above the screen: sheets, menus, the composer. */
  elevatedSurface: string
  /** The person's own message. */
  userBubble: string
  /** The field people type into. */
  inputBackground: string
  /** Hairlines between rows of one group. Quieter than `border`, which frames a whole block. */
  separator: string
  border: string
  borderSubtle: string
  accent: string
  accentPressed: string
  accentMuted: string
  accentText: string
  onAccent: string
  textPrimary: string
  textSecondary: string
  textMuted: string
  chromeHigh: string
  chromeMid: string
  chromeLow: string
  success: string
  warning: string
  error: string
  errorMuted: string
  warningMuted: string
  overlay: string
}

const dark: Palette = {
  bg: '#07080a',
  surface1: '#0e1013',
  surface2: '#14171b',
  surface3: '#1b1f24',
  surface4: '#242930',
  elevatedSurface: '#14171b',
  userBubble: '#1b1f24',
  inputBackground: '#14171b',
  separator: '#1b1f24',
  border: '#23282f',
  borderSubtle: '#171b20',
  accent: '#00c2d4',
  accentPressed: '#00a5b5',
  accentMuted: 'rgba(0, 194, 212, 0.12)',
  accentText: '#5ee4f2',
  onAccent: '#07080a',
  textPrimary: '#f2f5f8',
  textSecondary: '#a6aeb8',
  textMuted: '#7e868f',
  chromeHigh: '#f4f7fa',
  chromeMid: '#b9c2cc',
  chromeLow: '#9aa4b0',
  success: '#10b981',
  warning: '#f59e0b',
  error: '#ef4444',
  errorMuted: 'rgba(239, 68, 68, 0.12)',
  warningMuted: 'rgba(245, 158, 11, 0.12)',
  overlay: 'rgba(0, 0, 0, 0.6)',
}

// The Web's "paper" surfaces. accentText is darkened so cyan type on paper
// clears WCAG AA (5.1:1 on #eef1f4); plain #00c2d4 measures about 2:1 there.
const light: Palette = {
  bg: '#eef1f4',
  surface1: '#f7f9fa',
  surface2: '#e3e8ec',
  surface3: '#d8dee3',
  surface4: '#cfd6dc',
  elevatedSurface: '#ffffff',
  userBubble: '#e3e8ec',
  inputBackground: '#ffffff',
  separator: '#e3e8ec',
  border: '#cfd6dc',
  borderSubtle: '#e3e8ec',
  accent: '#00c2d4',
  accentPressed: '#00a5b5',
  accentMuted: 'rgba(0, 194, 212, 0.14)',
  accentText: '#006f7a',
  onAccent: '#07080a',
  textPrimary: '#0b0e11',
  textSecondary: '#39424c',
  textMuted: '#5a6570',
  chromeHigh: '#6f7a86',
  chromeMid: '#39424c',
  chromeLow: '#1b2027',
  success: '#047857',
  warning: '#b45309',
  error: '#b91c1c',
  errorMuted: 'rgba(185, 28, 28, 0.10)',
  warningMuted: 'rgba(180, 83, 9, 0.10)',
  overlay: 'rgba(11, 14, 17, 0.45)',
}

export const palettes: Record<ColorScheme, Palette> = { dark, light }

export const radius = { sm: 6, md: 10, lg: 14, xl: 20, full: 9999 } as const

export const space = { xs: 4, sm: 8, md: 12, lg: 16, xl: 24, xxl: 32, xxxl: 48 } as const

export const font = {
  mono: Platform.select({ ios: 'Menlo', android: 'monospace', default: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace' }),
  size: { xs: 12, sm: 14, md: 16, lg: 18, xl: 22, xxl: 28, display: 34 },
  weight: { regular: '400', medium: '500', semibold: '600', bold: '700' },
} as const

/**
 * Screen titles: `large` opens a page that is about one thing (Help, a plan), `header` names the screen in its bar.
 * Two sizes, so a title never has to be picked by eye.
 */
export const screenTitle = { large: 28, header: 17 } as const

/** Every icon is one of these three. Anything bigger is an illustration and sits inside a tile. */
export const iconSize = { sm: 16, md: 20, lg: 24 } as const

/** Motion, in milliseconds: `fast` for press feedback, `base` for sheets and panels, `slow` for loading shimmer. */
export const motion = { fast: 120, base: 220, slow: 900 } as const

/** Minimum touch target (Apple HIG 44pt, Material 48dp). */
export const TOUCH_TARGET = 44

/** Width at which layouts switch to the tablet / desktop arrangement. */
export const WIDE_BREAKPOINT = 768
