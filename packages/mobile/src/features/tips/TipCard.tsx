import Ionicons from '@expo/vector-icons/Ionicons'
import { StyleSheet, View } from 'react-native'

import { AppText, TextLink, type IconName } from '@/components/ui'
import { useT } from '@/i18n'
import { iconSize, radius, space, useTheme } from '@/theme'

import type { Tip, TipKind } from './tips'

const ICONS: Record<TipKind, IconName> = {
  didYouKnow: 'bulb-outline',
  tip: 'sparkles-outline',
  try: 'flask-outline',
  create: 'color-wand-outline',
}

export function TipCard({ tip }: { tip: Tip }) {
  const { colors } = useTheme()
  const t = useT()
  return (
    <View style={[styles.card, { backgroundColor: colors.elevatedSurface, borderColor: colors.borderSubtle }]} accessibilityRole="summary">
      <Ionicons name={ICONS[tip.kind]} size={iconSize.md} color={colors.accentText} style={styles.icon} />
      <View style={styles.body}>
        <AppText variant="label" tone="accent">
          {t(`tips.label.${tip.kind}`)}
        </AppText>
        <AppText variant="caption" tone="secondary">
          {t(tip.textKey)}
        </AppText>
        {tip.action ? <TextLink label={t(tip.action.labelKey)} href={tip.action.href} /> : null}
      </View>
    </View>
  )
}

const styles = StyleSheet.create({
  card: { flexDirection: 'row', gap: space.md, padding: space.md, borderRadius: radius.lg, borderWidth: StyleSheet.hairlineWidth },
  icon: { marginTop: 2 },
  body: { flex: 1, gap: 2 },
})
