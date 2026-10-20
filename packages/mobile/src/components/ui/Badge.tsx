import { StyleSheet, View } from 'react-native'

import { radius, space, useTheme } from '@/theme'

import { AppText } from './AppText'

export type BadgeTone = 'neutral' | 'accent' | 'success' | 'warning' | 'error'

export function Badge({ label, tone = 'neutral' }: { label: string; tone?: BadgeTone }) {
  const { colors } = useTheme()
  const palette = {
    neutral: { bg: colors.surface3, fg: colors.textSecondary },
    accent: { bg: colors.accentMuted, fg: colors.accentText },
    success: { bg: colors.surface2, fg: colors.success },
    warning: { bg: colors.warningMuted, fg: colors.warning },
    error: { bg: colors.errorMuted, fg: colors.error },
  }[tone]
  return (
    <View style={[styles.badge, { backgroundColor: palette.bg }]}>
      <AppText variant="caption" numberOfLines={1} style={[styles.text, { color: palette.fg }]}>
        {label}
      </AppText>
    </View>
  )
}

const styles = StyleSheet.create({
  badge: { borderRadius: radius.full, paddingHorizontal: space.sm, paddingVertical: 2, alignSelf: 'flex-start' },
  text: { fontSize: 12, lineHeight: 18, fontWeight: '600' },
})
