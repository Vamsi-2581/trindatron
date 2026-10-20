import { Text, type TextProps, type TextStyle } from 'react-native'

import { font, screenTitle, useTheme, type Palette } from '@/theme'

export type TextVariant =
  | 'display'
  | 'screenTitle'
  | 'title'
  | 'heading'
  | 'headerTitle'
  | 'body'
  | 'bodyStrong'
  | 'caption'
  | 'sectionTitle'
  | 'label'
  | 'mono'
export type TextTone = 'primary' | 'secondary' | 'muted' | 'accent' | 'error' | 'success' | 'warning' | 'onAccent'

const VARIANTS: Record<TextVariant, TextStyle> = {
  display: { fontSize: font.size.display, lineHeight: 40, fontWeight: font.weight.semibold, letterSpacing: -0.4 },
  screenTitle: { fontSize: screenTitle.large, lineHeight: 34, fontWeight: font.weight.semibold, letterSpacing: -0.3 },
  title: { fontSize: font.size.xxl, lineHeight: 34, fontWeight: font.weight.semibold, letterSpacing: -0.2 },
  heading: { fontSize: font.size.lg, lineHeight: 24, fontWeight: font.weight.semibold },
  headerTitle: { fontSize: screenTitle.header, lineHeight: 22, fontWeight: font.weight.semibold },
  body: { fontSize: font.size.md, lineHeight: 24 },
  bodyStrong: { fontSize: font.size.md, lineHeight: 24, fontWeight: font.weight.semibold },
  caption: { fontSize: font.size.sm, lineHeight: 20 },
  // A group's name, written the way it is said: quieter than a heading, never shouted in capitals.
  sectionTitle: { fontSize: font.size.sm, lineHeight: 20, fontWeight: font.weight.semibold },
  // Same rule as sectionTitle: "Plano atual", not "PLANO ATUAL" — capitals read as shouting on Cobrança, Uso and Planos.
  label: { fontSize: font.size.xs, lineHeight: 16, fontWeight: font.weight.semibold },
  mono: { fontSize: font.size.sm, lineHeight: 20, fontFamily: font.mono },
}

function toneColor(colors: Palette, tone: TextTone): string {
  switch (tone) {
    case 'secondary':
      return colors.textSecondary
    case 'muted':
      return colors.textMuted
    case 'accent':
      return colors.accentText
    case 'error':
      return colors.error
    case 'success':
      return colors.success
    case 'warning':
      return colors.warning
    case 'onAccent':
      return colors.onAccent
    default:
      return colors.textPrimary
  }
}

export interface AppTextProps extends TextProps {
  variant?: TextVariant
  tone?: TextTone
}

export function AppText({ variant = 'body', tone, style, maxFontSizeMultiplier = 1.8, ...rest }: AppTextProps) {
  const { colors } = useTheme()
  const fallbackTone: TextTone =
    variant === 'caption' || variant === 'label' ? 'secondary' : variant === 'sectionTitle' ? 'muted' : 'primary'
  return (
    <Text
      maxFontSizeMultiplier={maxFontSizeMultiplier}
      style={[VARIANTS[variant], { color: toneColor(colors, tone ?? fallbackTone) }, style]}
      {...rest}
    />
  )
}
