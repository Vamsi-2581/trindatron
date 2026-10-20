import type { ReactNode } from 'react'
import { ScrollView, StyleSheet, View } from 'react-native'
import { KeyboardAwareScrollView } from 'react-native-keyboard-controller'
import { SafeAreaView } from 'react-native-safe-area-context'

import { AppText, BrandSymbol, Wordmark } from '@/components/ui'
import { space, useTheme } from '@/theme'

export function AuthShell({ title, subtitle, children }: { title: string; subtitle?: string; children: ReactNode }) {
  const { colors } = useTheme()
  const content = (
    <View style={styles.column}>
      <View style={styles.brand} accessibilityRole="header" accessibilityLabel="TRINDATRON">
        <BrandSymbol size={96} />
        <Wordmark size={22} />
      </View>
      <View style={styles.heading}>
        <AppText variant="title" accessibilityRole="header">
          {title}
        </AppText>
        {subtitle ? <AppText tone="secondary">{subtitle}</AppText> : null}
      </View>
      {children}
    </View>
  )

  return (
    <SafeAreaView edges={['top', 'bottom', 'left', 'right']} style={[styles.flex, { backgroundColor: colors.bg }]}>
      {/* Scrolls the focused field above the keyboard on every platform, with no fixed offsets. */}
      <KeyboardAwareScrollView contentContainerStyle={styles.scroll} keyboardShouldPersistTaps="handled" bottomOffset={space.xl}>
        {content}
      </KeyboardAwareScrollView>
    </SafeAreaView>
  )
}

export const AuthScroll = ScrollView

const styles = StyleSheet.create({
  flex: { flex: 1 },
  scroll: { flexGrow: 1, justifyContent: 'center', paddingHorizontal: space.lg, paddingVertical: space.xxl },
  column: { width: '100%', maxWidth: 420, alignSelf: 'center', gap: space.lg },
  brand: { alignItems: 'center', gap: space.md, marginBottom: space.md },
  heading: { gap: space.xs },
})
