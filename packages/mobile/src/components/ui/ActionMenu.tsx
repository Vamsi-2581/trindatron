import { MenuView } from '@expo/ui/community/menu'
import Ionicons from '@expo/vector-icons/Ionicons'
import { useState } from 'react'
import { Platform, StyleSheet, View } from 'react-native'

import { iconSize, radius, TOUCH_TARGET, useTheme } from '@/theme'

import { IconButton, type IconName } from './Button'
import { BottomSheet, SheetOption } from './Sheet'

export interface ActionMenuItem {
  id: string
  title: string
  icon?: IconName
  destructive?: boolean
}

/**
 * The "⋯" of a row or a screen: a few actions that do not deserve a button of their own.
 *
 * On iOS it is @expo/ui's MenuView (MIT), SwiftUI's Menu anchored to the trigger. The Web and Android open the same
 * actions in the app's bottom sheet: @expo/ui does not fire menu actions on the Web, and on Android its MenuView is a
 * Jetpack Compose host (`Host matchContents`) whose synchronous layout event, with Reanimated listening, crashed a
 * physical Android 16 device during navigation (expo/expo#47625) — and the drawer has one per conversation.
 */
export function ActionMenu({
  label,
  items,
  onSelect,
  icon = 'ellipsis-horizontal',
}: {
  /** Names the trigger for assistive technology ("Ações da conversa X"). */
  label: string
  items: ActionMenuItem[]
  onSelect: (id: string) => void
  icon?: IconName
}) {
  const { colors, scheme } = useTheme()
  const [open, setOpen] = useState(false)

  if (Platform.OS !== 'ios') {
    return (
      <>
        <IconButton icon={icon} label={label} size="md" onPress={() => setOpen(true)} />
        <BottomSheet visible={open} onClose={() => setOpen(false)}>
          {items.map((item) => (
            <SheetOption
              key={item.id}
              icon={item.icon}
              label={item.title}
              destructive={item.destructive}
              onPress={() => {
                setOpen(false)
                onSelect(item.id)
              }}
            />
          ))}
        </BottomSheet>
      </>
    )
  }

  return (
    <MenuView
      colorScheme={scheme}
      actions={items.map((item) => ({
        id: item.id,
        title: item.title,
        titleColor: item.destructive ? colors.error : colors.textPrimary,
        attributes: item.destructive ? { destructive: true } : undefined,
      }))}
      onPressAction={(event) => onSelect(event.nativeEvent.event)}
    >
      {/* A plain view: the menu owns the press, a nested pressable would swallow it. */}
      <View accessible accessibilityRole="button" accessibilityLabel={label} style={styles.trigger}>
        <Ionicons name={icon} size={iconSize.md} color={colors.textSecondary} />
      </View>
    </MenuView>
  )
}

const styles = StyleSheet.create({
  trigger: { width: TOUCH_TARGET, height: TOUCH_TARGET, borderRadius: radius.md, alignItems: 'center', justifyContent: 'center' },
})
