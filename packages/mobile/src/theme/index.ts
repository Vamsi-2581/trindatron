import { useColorScheme, useWindowDimensions } from 'react-native'

import { palettes, WIDE_BREAKPOINT, type ColorScheme, type Palette } from './tokens'

export * from './tokens'

export function useTheme(): { scheme: ColorScheme; colors: Palette } {
  const scheme: ColorScheme = useColorScheme() === 'light' ? 'light' : 'dark'
  return { scheme, colors: palettes[scheme] }
}

export function useIsWide(): boolean {
  const { width } = useWindowDimensions()
  return width >= WIDE_BREAKPOINT
}
