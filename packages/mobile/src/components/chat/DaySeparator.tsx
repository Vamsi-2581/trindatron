import { StyleSheet, View } from 'react-native'

import { AppText } from '@/components/ui'
import { dayLabel } from '@/features/chat/dayLabel'
import { INTL_LOCALE, useLocale, useT } from '@/i18n'
import { radius, space, useTheme } from '@/theme'

/** Marks where a new day starts in a conversation, so history reopened later reads in time. */
export function DaySeparator({ createdAt }: { createdAt: Date | number }) {
  const { colors } = useTheme()
  const t = useT()
  const locale = useLocale()
  const label = dayLabel(createdAt, new Date(), INTL_LOCALE[locale], { today: t('chat.today'), yesterday: t('chat.yesterday') })
  if (!label) return null
  return (
    <View style={styles.row}>
      <View style={[styles.pill, { backgroundColor: colors.surface2, borderColor: colors.border }]}>
        <AppText variant="caption" tone="secondary">
          {label}
        </AppText>
      </View>
    </View>
  )
}

const styles = StyleSheet.create({
  row: { alignItems: 'center', paddingTop: space.md, paddingBottom: space.xs },
  pill: { borderRadius: radius.full, borderWidth: StyleSheet.hairlineWidth, paddingHorizontal: space.md, paddingVertical: space.xs },
})
