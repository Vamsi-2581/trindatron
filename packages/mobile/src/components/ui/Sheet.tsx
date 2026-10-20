import NativeBottomSheet, { BottomSheetView } from '@expo/ui/community/bottom-sheet'
import Ionicons from '@expo/vector-icons/Ionicons'
import { useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { Modal, Platform, Pressable, StyleSheet, View } from 'react-native'
import { SafeAreaInsetsContext } from 'react-native-safe-area-context'

import { useT } from '@/i18n'
import { iconSize, motion, radius, space, TOUCH_TARGET, useTheme } from '@/theme'

import { AppText } from './AppText'
import type { IconName } from './Button'

interface BottomSheetProps {
  visible: boolean
  onClose: () => void
  title?: string
  children: ReactNode
}

/**
 * A short list of choices that slides up from the bottom, instead of a screen-sized card.
 *
 * iOS and the Web use @expo/ui's sheet (MIT): a SwiftUI sheet, and a vaul drawer on the Web. Android does not: its
 * @expo/ui sheet hosts the content in Jetpack Compose (`RNHostView matchContents`), and Compose hosts emit their
 * layout event synchronously during measure; with Reanimated listening, a navigation crashes the app with
 * "performMeasureAndLayout called during measure layout" (seen on a physical Android 16 device; expo/expo#47625).
 * So Android gets a React Native modal sheet with the same API: `visible`, `onClose`, an optional title and options.
 */
export function BottomSheet(props: BottomSheetProps) {
  return Platform.OS === 'android' ? <ModalSheet {...props} /> : <ExpoSheet {...props} />
}

/** Android: a plain modal. Closes on the scrim and on the system back action; clears the navigation bar itself. */
function ModalSheet({ visible, onClose, title, children }: BottomSheetProps) {
  const { colors } = useTheme()
  const t = useT()
  // A sheet can open from a component rendered outside the app's SafeAreaProvider; then there is no inset to add.
  const bottomInset = useContext(SafeAreaInsetsContext)?.bottom ?? 0
  return (
    <Modal
      visible={visible}
      transparent
      animationType="fade"
      onRequestClose={onClose}
      statusBarTranslucent
      navigationBarTranslucent
    >
      <View style={styles.modalRoot}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={t('common.close')}
          onPress={onClose}
          style={[StyleSheet.absoluteFill, { backgroundColor: colors.overlay }]}
        />
        <View
          accessibilityViewIsModal
          style={[
            styles.modalSheet,
            { backgroundColor: colors.elevatedSurface, borderColor: colors.border, paddingBottom: space.lg + bottomInset },
          ]}
        >
          <View style={[styles.handle, { backgroundColor: colors.surface4 }]} />
          <View style={styles.content}>
            {title ? (
              <AppText variant="sectionTitle" accessibilityRole="header" style={styles.title}>
                {title}
              </AppText>
            ) : null}
            {children}
          </View>
        </View>
      </View>
    </Modal>
  )
}

function ExpoSheet({ visible, onClose, title, children }: BottomSheetProps) {
  const { colors } = useTheme()
  const sheet = useRef<NativeBottomSheet>(null)
  const [mounted, setMounted] = useState(visible)
  // Opening mounts the sheet right away, while rendering; closing lets it animate out first.
  if (visible && !mounted) setMounted(true)
  const wantsOpen = useRef(visible)
  // Declared before the closing effect so a dismissal the screen asked for is not reported back to it.
  useEffect(() => {
    wantsOpen.current = visible
  }, [visible])

  useEffect(() => {
    if (visible || !mounted) return
    sheet.current?.close()
    // The native sheet reports its own dismissal; this only guards against one that never does.
    const fallback = setTimeout(() => setMounted(false), motion.base * 2)
    return () => clearTimeout(fallback)
  }, [visible, mounted])

  if (!mounted) return null
  return (
    <NativeBottomSheet
      ref={sheet}
      index={0}
      enablePanDownToClose
      onClose={() => {
        setMounted(false)
        // Dismissed by the person (drag, scrim, back), not by the screen: tell the screen.
        if (wantsOpen.current) onClose()
      }}
      backgroundStyle={{ backgroundColor: colors.elevatedSurface }}
      handleIndicatorStyle={{ backgroundColor: colors.surface4 }}
    >
      <BottomSheetView style={styles.sheet}>
        <View accessibilityViewIsModal style={styles.content}>
          {title ? (
            <AppText variant="sectionTitle" accessibilityRole="header" style={styles.title}>
              {title}
            </AppText>
          ) : null}
          {children}
        </View>
      </BottomSheetView>
    </NativeBottomSheet>
  )
}

export function SheetOption({
  icon,
  label,
  detail,
  onPress,
  selected = false,
  disabled = false,
  destructive = false,
}: {
  icon?: IconName
  label: string
  detail?: string | null
  onPress: () => void
  selected?: boolean
  disabled?: boolean
  destructive?: boolean
}) {
  const { colors } = useTheme()
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ selected, disabled }}
      accessibilityLabel={detail ? `${label}, ${detail}` : label}
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [styles.option, { backgroundColor: pressed ? colors.surface2 : 'transparent', opacity: disabled ? 0.45 : 1 }]}
    >
      {icon ? (
        <View style={[styles.optionIcon, { backgroundColor: destructive ? colors.errorMuted : colors.surface2 }]}>
          <Ionicons name={icon} size={iconSize.md} color={destructive ? colors.error : colors.textPrimary} />
        </View>
      ) : null}
      <View style={styles.flex}>
        <AppText variant="bodyStrong" numberOfLines={1} tone={destructive ? 'error' : 'primary'}>
          {label}
        </AppText>
        {detail ? (
          <AppText variant="caption" tone="muted" numberOfLines={2}>
            {detail}
          </AppText>
        ) : null}
      </View>
      {selected ? <Ionicons name="checkmark" size={iconSize.md} color={colors.accentText} /> : null}
    </Pressable>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  sheet: { width: '100%', maxWidth: 560, alignSelf: 'center' },
  modalRoot: { flex: 1, justifyContent: 'flex-end' },
  modalSheet: {
    width: '100%',
    maxWidth: 560,
    alignSelf: 'center',
    borderTopLeftRadius: radius.xl,
    borderTopRightRadius: radius.xl,
    borderWidth: StyleSheet.hairlineWidth,
    borderBottomWidth: 0,
    paddingTop: space.sm,
  },
  handle: { width: 36, height: 4, borderRadius: 2, alignSelf: 'center', marginBottom: space.xs },
  content: { paddingHorizontal: space.sm, paddingTop: space.xs, paddingBottom: space.lg, gap: 2 },
  title: { paddingHorizontal: space.md, paddingVertical: space.sm },
  option: {
    minHeight: TOUCH_TARGET + 8,
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    paddingHorizontal: space.md,
    borderRadius: radius.md,
  },
  optionIcon: { width: 36, height: 36, borderRadius: radius.full, alignItems: 'center', justifyContent: 'center' },
})
