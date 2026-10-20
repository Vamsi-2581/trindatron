import { Image } from 'expo-image'
import { PixelRatio, Platform, StyleSheet, View } from 'react-native'

import { palettes, space, useTheme } from '@/theme'

/**
 * The official TRINDATRON artwork. Every file below comes from the master
 * (assets/brand/trindatron-symbol.png) through scripts/brand/derive_brand_assets.py:
 * re-matted, edge band cleaned so the chrome has no light halo on dark surfaces,
 * then only downscaled — never redrawn, never upscaled. docs/BRAND_SYSTEM.md explains each use.
 */
export const SYMBOL_SIZE = { width: 573, height: 541 } as const
export const SYMBOL_RATIO = SYMBOL_SIZE.width / SYMBOL_SIZE.height
export const WORDMARK_SIZE = { width: 798, height: 82 } as const
export const WORDMARK_RATIO = WORDMARK_SIZE.width / WORDMARK_SIZE.height

export const BRAND_ASSETS = {
  symbolHd: require('../../../assets/brand/trindatron-symbol-hd.png'),
  wordmark: require('../../../assets/brand/trindatron-wordmark.png'),
  introUpper: require('../../../assets/brand/trindatron-intro-upper.png'),
  introLower: require('../../../assets/brand/trindatron-intro-lower.png'),
  glow: require('../../../assets/brand/trindatron-glow.png'),
} as const

// Dense on purpose: a screen never shrinks a file by more than ~1.3x, because the GPU's
// plain downscale is what made the thin cyan lines look jagged in the header.
const SYMBOL_ASSETS = [
  { width: 48, source: require('../../../assets/brand/trindatron-symbol-48.png') },
  { width: 64, source: require('../../../assets/brand/trindatron-symbol-64.png') },
  { width: 80, source: require('../../../assets/brand/trindatron-symbol-80.png') },
  { width: 96, source: require('../../../assets/brand/trindatron-symbol-96.png') },
  { width: 128, source: require('../../../assets/brand/trindatron-symbol-128.png') },
  { width: 160, source: require('../../../assets/brand/trindatron-symbol-160.png') },
  { width: 192, source: require('../../../assets/brand/trindatron-symbol-192.png') },
  { width: 256, source: require('../../../assets/brand/trindatron-symbol-256.png') },
  { width: 320, source: require('../../../assets/brand/trindatron-symbol-320.png') },
  { width: 384, source: require('../../../assets/brand/trindatron-symbol-384.png') },
  { width: 512, source: require('../../../assets/brand/trindatron-symbol-512.png') },
  { width: SYMBOL_SIZE.width, source: BRAND_ASSETS.symbolHd },
] as const

/** Smallest official file with enough pixels for `heightPt` at the screen's density. */
export function pickSymbolAsset(heightPt: number, pixelRatio: number = PixelRatio.get()) {
  const neededWidth = Math.ceil(heightPt * SYMBOL_RATIO * pixelRatio)
  return SYMBOL_ASSETS.find((asset) => asset.width >= neededWidth) ?? SYMBOL_ASSETS[SYMBOL_ASSETS.length - 1]!
}

/** Up to this size the chrome's highlights are too thin to separate from a light page by themselves. */
export const SYMBOL_TILE_MAX = 40

export function BrandSymbol({ size = 28 }: { size?: number }) {
  const { scheme } = useTheme()
  const image = (
    <Image
      source={pickSymbolAsset(size).source}
      style={{ height: size, width: Math.round(size * SYMBOL_RATIO) }}
      contentFit="contain"
      accessible={false}
    />
  )
  // The chrome is lit for a dark ground (the master's). On the light theme a small symbol keeps that ground as a
  // plain tile — the brand system allows the mark on flat dark or light, never on texture — instead of being
  // repainted or losing its highlights against paper.
  if (scheme !== 'light' || size > SYMBOL_TILE_MAX) return image
  const pad = Math.max(3, Math.round(size * 0.18))
  return (
    <View style={[styles.tile, { padding: pad, borderRadius: Math.round((size + pad * 2) * 0.28) }]} testID="brand-symbol-tile">
      {image}
    </View>
  )
}

/**
 * Colours the white wordmark file. Native uses the image tint; on the Web the
 * image tint relies on an SVG filter that some browsers drop (the wordmark then
 * disappears), so the Web keeps the white file on dark and darkens it on light.
 */
export function wordmarkTint(scheme: 'dark' | 'light', color: string) {
  if (Platform.OS !== 'web') return { tintColor: color, style: null }
  return { tintColor: undefined, style: scheme === 'light' ? ({ filter: 'brightness(0)' } as object) : null }
}

/** The official wordmark, cut from the master lockup and tinted with the theme's text colour. `size` is the cap height. */
export function Wordmark({ size = 12 }: { size?: number }) {
  const { colors, scheme } = useTheme()
  const tint = wordmarkTint(scheme, colors.textPrimary)
  return (
    <View accessible accessibilityRole="image" accessibilityLabel="TRINDATRON">
      <Image
        source={BRAND_ASSETS.wordmark}
        tintColor={tint.tintColor}
        style={[{ height: size, width: Math.round(size * WORDMARK_RATIO) }, tint.style]}
        contentFit="contain"
        accessible={false}
      />
    </View>
  )
}

export function BrandLockup({ size = 'md' }: { size?: 'sm' | 'md' | 'lg' }) {
  const symbol = size === 'sm' ? 26 : size === 'lg' ? 56 : 36
  const text = size === 'sm' ? 11 : size === 'lg' ? 18 : 14
  return (
    <View style={styles.lockup} accessible accessibilityRole="header" accessibilityLabel="TRINDATRON">
      <BrandSymbol size={symbol} />
      <Wordmark size={text} />
    </View>
  )
}

const styles = StyleSheet.create({
  lockup: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  // The splash and adaptive-icon ground (docs/BRAND_SYSTEM.md), the colour the chrome was lit against.
  tile: { backgroundColor: palettes.dark.bg, alignItems: 'center', justifyContent: 'center' },
})
