import Ionicons from '@expo/vector-icons/Ionicons'
import { useContext, type ReactNode } from 'react'
import {
  ActivityIndicator,
  Pressable,
  RefreshControl,
  StyleSheet,
  View,
  type StyleProp,
  type ViewStyle,
} from 'react-native'
import { KeyboardAwareScrollView } from 'react-native-keyboard-controller'
import { SafeAreaInsetsContext, SafeAreaView, type Edge } from 'react-native-safe-area-context'

import { useIsOffline } from '@/hooks/useNetworkStatus'
import { useT } from '@/i18n'
import { iconSize, radius, space, TOUCH_TARGET, useTheme } from '@/theme'

import { AppText } from './AppText'
import { Button, type IconName } from './Button'

export const CONTENT_MAX_WIDTH = 760

export function OfflineBanner() {
  const { colors } = useTheme()
  const t = useT()
  return (
    <View
      accessibilityRole="alert"
      accessibilityLiveRegion="polite"
      style={[styles.banner, { backgroundColor: colors.warningMuted, borderColor: colors.warning }]}
    >
      <Ionicons name="cloud-offline-outline" size={iconSize.sm} color={colors.warning} />
      <AppText variant="caption" tone="warning" style={styles.flex}>
        {t('states.offline')}
      </AppText>
    </View>
  )
}

export interface ScreenProps {
  children: ReactNode
  scroll?: boolean
  refreshing?: boolean
  onRefresh?: () => void
  edges?: Edge[]
  contentStyle?: StyleProp<ViewStyle>
}

/**
 * Every screen but the conversation: safe area, offline banner, and the scroll everything else sits in.
 *
 * Two things every screen needs, and neither can be left to the screens themselves:
 *
 * The navigation bar. Android draws content behind it (edge-to-edge is enforced from Android 15), so the room it
 * takes is reserved *inside* the scroll instead of as a safe-area edge: content still scrolls under the translucent
 * bar, and the end of the list — the last row, the last button — lands above it. Screens used to rely on the tab bar
 * for this, and lost it when the tabs became the side menu; the last ticket in Support and "Entrar no time" ended up
 * under the bar, unreachable.
 *
 * The keyboard. Also from Android 15 the window no longer resizes for the keyboard, so a plain ScrollView leaves the
 * focused field behind it — measured at 470 px behind on a physical device. KeyboardAwareScrollView brings the
 * focused input above the keyboard on every platform.
 */
export function Screen({ children, scroll = true, refreshing = false, onRefresh, edges = ['left', 'right'], contentStyle }: ScreenProps) {
  const { colors } = useTheme()
  const offline = useIsOffline()
  // Read, not required: a screen can be rendered outside a provider (tests, a modal host), and then there is no inset.
  const bottomInset = useContext(SafeAreaInsetsContext)?.bottom ?? 0
  // The bottom is handled as content padding below, never as a safe-area edge that would cut the scroll short.
  const safeEdges = edges.filter((edge) => edge !== 'bottom')
  const content = <View style={[styles.content, !scroll && styles.flex, contentStyle]}>{children}</View>

  return (
    <SafeAreaView edges={safeEdges} style={[styles.flex, { backgroundColor: colors.bg }]}>
      {offline ? <OfflineBanner /> : null}
      {scroll ? (
        <KeyboardAwareScrollView
          bottomOffset={space.lg}
          contentContainerStyle={[styles.scroll, { paddingBottom: space.xl + bottomInset }]}
          keyboardShouldPersistTaps="handled"
          refreshControl={
            onRefresh ? (
              <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.accent} colors={[colors.accent]} />
            ) : undefined
          }
        >
          {content}
        </KeyboardAwareScrollView>
      ) : (
        <View style={[styles.flex, { paddingBottom: bottomInset }]}>{content}</View>
      )}
    </SafeAreaView>
  )
}

export function Section({ title, action, children }: { title: string; action?: ReactNode; children: ReactNode }) {
  return (
    <View style={styles.section}>
      <View style={styles.sectionHeader}>
        <AppText variant="sectionTitle" accessibilityRole="header">
          {title}
        </AppText>
        {action}
      </View>
      {children}
    </View>
  )
}

export function Surface({ children, style }: { children: ReactNode; style?: StyleProp<ViewStyle> }) {
  const { colors } = useTheme()
  return (
    <View style={[styles.surface, { backgroundColor: colors.elevatedSurface, borderColor: colors.borderSubtle }, style]}>
      {children}
    </View>
  )
}

export function ListRow({
  title,
  subtitle,
  icon,
  trailing,
  onPress,
  onLongPress,
  accessibilityHint,
  destructive = false,
}: {
  title: string
  subtitle?: string | null
  icon?: IconName
  trailing?: ReactNode
  onPress?: () => void
  onLongPress?: () => void
  accessibilityHint?: string
  destructive?: boolean
}) {
  const { colors } = useTheme()
  return (
    <Pressable
      accessibilityRole={onPress ? 'button' : undefined}
      accessibilityHint={accessibilityHint}
      disabled={!onPress && !onLongPress}
      onPress={onPress}
      onLongPress={onLongPress}
      style={({ pressed }) => [styles.row, { backgroundColor: pressed ? colors.surface2 : 'transparent' }]}
    >
      {icon ? <Ionicons name={icon} size={iconSize.md} color={destructive ? colors.error : colors.accentText} /> : null}
      <View style={styles.flex}>
        <AppText variant="bodyStrong" numberOfLines={1} tone={destructive ? 'error' : 'primary'}>
          {title}
        </AppText>
        {subtitle ? (
          <AppText variant="caption" tone="muted" numberOfLines={2}>
            {subtitle}
          </AppText>
        ) : null}
      </View>
      {trailing ?? (onPress ? <Ionicons name="chevron-forward" size={iconSize.sm} color={colors.textMuted} /> : null)}
    </Pressable>
  )
}

export function Divider() {
  const { colors } = useTheme()
  return <View style={{ height: StyleSheet.hairlineWidth, backgroundColor: colors.separator }} />
}

export function LoadingState({ label }: { label?: string }) {
  const { colors } = useTheme()
  const t = useT()
  const text = label ?? t('common.loading')
  return (
    <View style={styles.state} accessibilityRole="progressbar" accessibilityLabel={text}>
      <ActivityIndicator color={colors.accent} />
      <AppText variant="caption" tone="muted">
        {text}
      </AppText>
    </View>
  )
}

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  const { colors } = useTheme()
  const t = useT()
  return (
    <View style={styles.state} accessibilityRole="alert">
      <View style={[styles.stateIcon, { backgroundColor: colors.errorMuted }]}>
        <Ionicons name="alert-circle-outline" size={iconSize.lg} color={colors.error} />
      </View>
      <AppText variant="body" tone="secondary" style={styles.center}>
        {message}
      </AppText>
      {onRetry ? <Button label={t('common.retry')} variant="secondary" icon="refresh" onPress={onRetry} /> : null}
    </View>
  )
}

/**
 * The one way a screen says it has nothing yet: the icon on its tile, what is missing, why, and — always in this
 * place — the single action that fills it. Every screen uses it, so an empty screen looks the same everywhere.
 */
export function EmptyState({ icon, title, body, action }: { icon: IconName; title: string; body?: string; action?: ReactNode }) {
  const { colors } = useTheme()
  return (
    <View style={styles.state}>
      <View style={[styles.stateIcon, { backgroundColor: colors.surface2 }]}>
        <Ionicons name={icon} size={iconSize.lg} color={colors.textSecondary} />
      </View>
      <View style={styles.stateText}>
        <AppText variant="bodyStrong" style={styles.center}>
          {title}
        </AppText>
        {body ? (
          <AppText variant="caption" tone="muted" style={styles.center}>
            {body}
          </AppText>
        ) : null}
      </View>
      {action}
    </View>
  )
}

export function ProgressBar({ value, label }: { value: number; label: string }) {
  const { colors } = useTheme()
  const clamped = Math.max(0, Math.min(100, value))
  return (
    <View
      accessibilityRole="progressbar"
      accessibilityLabel={label}
      accessibilityValue={{ min: 0, max: 100, now: Math.round(clamped) }}
      style={[styles.track, { backgroundColor: colors.surface3 }]}
    >
      <View style={[styles.fill, { width: `${clamped}%`, backgroundColor: clamped >= 90 ? colors.warning : colors.accent }]} />
    </View>
  )
}

export function Notice({ tone = 'warning', children }: { tone?: 'warning' | 'error' | 'info'; children: ReactNode }) {
  const { colors } = useTheme()
  const palette = {
    warning: { bg: colors.warningMuted, fg: colors.warning, icon: 'information-circle-outline' as const },
    error: { bg: colors.errorMuted, fg: colors.error, icon: 'alert-circle-outline' as const },
    info: { bg: colors.accentMuted, fg: colors.accentText, icon: 'information-circle-outline' as const },
  }[tone]
  return (
    <View accessibilityRole={tone === 'error' ? 'alert' : undefined} style={[styles.notice, { backgroundColor: palette.bg }]}>
      <Ionicons name={palette.icon} size={iconSize.md} color={palette.fg} />
      <View style={styles.flex}>{typeof children === 'string' ? <AppText variant="caption" style={{ color: palette.fg }}>{children}</AppText> : children}</View>
    </View>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  center: { textAlign: 'center' },
  scroll: { flexGrow: 1 },
  content: {
    width: '100%',
    maxWidth: CONTENT_MAX_WIDTH,
    alignSelf: 'center',
    paddingHorizontal: space.lg,
    paddingVertical: space.lg,
    gap: space.xl,
  },
  banner: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    paddingHorizontal: space.lg,
    paddingVertical: space.sm,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  section: { gap: space.sm },
  sectionHeader: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', minHeight: 28 },
  surface: { borderRadius: radius.lg, borderWidth: StyleSheet.hairlineWidth, overflow: 'hidden' },
  row: {
    minHeight: TOUCH_TARGET + 12,
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    paddingHorizontal: space.lg,
    paddingVertical: space.sm,
  },
  state: { alignItems: 'center', justifyContent: 'center', gap: space.md, paddingVertical: space.xxl, paddingHorizontal: space.lg },
  stateIcon: { width: 48, height: 48, borderRadius: radius.full, alignItems: 'center', justifyContent: 'center' },
  stateText: { gap: space.xs, maxWidth: 360 },
  track: { height: 8, borderRadius: radius.full, overflow: 'hidden' },
  fill: { height: '100%', borderRadius: radius.full },
  notice: { flexDirection: 'row', gap: space.sm, padding: space.md, borderRadius: radius.md, alignItems: 'flex-start' },
})
