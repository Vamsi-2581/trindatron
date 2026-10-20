import { router } from 'expo-router'
import type { ReactNode } from 'react'
import { StyleSheet, View } from 'react-native'

import { useT } from '@/i18n'
import { space, TOUCH_TARGET, useTheme } from '@/theme'

import { AppText } from './AppText'
import { IconButton } from './Button'

/** Back to where the person came from, or Home when a link opened this screen directly. */
export function goBack() {
  if (router.canGoBack()) router.back()
  else router.replace('/')
}

/**
 * The one header every screen uses: what opens on the left (back, or the menu), the title beside it, and the
 * screen's own actions on the right. Replaces the chat's own header, the native stack header and Home's centred
 * lockup, which gave three different backs and three different title positions.
 */
export function AppHeader({
  title,
  leading = 'back',
  onBack = goBack,
  trailing,
  border = true,
}: {
  /** A string, or an element such as the brand lockup on Home. */
  title?: ReactNode
  /** `back`, nothing, or an element (the menu button). */
  leading?: 'back' | 'none' | ReactNode
  onBack?: () => void
  trailing?: ReactNode
  border?: boolean
}) {
  const { colors } = useTheme()
  const t = useT()
  return (
    <View
      style={[
        styles.header,
        { backgroundColor: colors.bg, borderBottomColor: colors.separator, borderBottomWidth: border ? StyleSheet.hairlineWidth : 0 },
      ]}
    >
      {leading === 'back' ? (
        <IconButton icon="chevron-back" label={t('common.back')} onPress={onBack} />
      ) : leading === 'none' ? (
        <View style={styles.inset} />
      ) : (
        leading
      )}
      <View style={styles.title}>
        {typeof title === 'string' ? (
          <AppText variant="headerTitle" numberOfLines={1} accessibilityRole="header">
            {title}
          </AppText>
        ) : (
          title
        )}
      </View>
      {trailing ? <View style={styles.trailing}>{trailing}</View> : null}
    </View>
  )
}

const styles = StyleSheet.create({
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.xs,
    minHeight: TOUCH_TARGET + space.sm,
    paddingHorizontal: space.xs,
  },
  inset: { width: space.sm },
  title: { flex: 1, minWidth: 0, justifyContent: 'center' },
  trailing: { flexDirection: 'row', alignItems: 'center' },
})
