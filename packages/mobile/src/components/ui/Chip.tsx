import Ionicons from '@expo/vector-icons/Ionicons'
import { Pressable, StyleSheet } from 'react-native'

import { iconSize, radius, space, useTheme } from '@/theme'

import { AppText } from './AppText'
import { PRESSED_SCALE, type IconName } from './Button'

/**
 * The one selectable pill: filters (file kinds, help categories), a choice between a few options (seat type),
 * and a suggestion to start from. It replaces four copies that had drifted apart.
 *
 * `role` says what pressing it means: `radio` picks one of a set, `button` does something (open a menu, start a
 * suggestion) and may carry a trailing icon.
 */
export function Chip({
  label,
  selected = false,
  onPress,
  icon,
  trailingIcon,
  role = 'radio',
  accessibilityLabel,
  disabled = false,
}: {
  label: string
  selected?: boolean
  onPress: () => void
  icon?: IconName
  trailingIcon?: IconName
  role?: 'radio' | 'button'
  accessibilityLabel?: string
  disabled?: boolean
}) {
  const { colors } = useTheme()
  const foreground = selected ? colors.accentText : colors.textSecondary
  return (
    <Pressable
      accessibilityRole={role}
      accessibilityState={role === 'radio' ? { selected, checked: selected, disabled } : { disabled }}
      accessibilityLabel={accessibilityLabel ?? label}
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [
        styles.chip,
        {
          borderColor: selected ? colors.accent : colors.border,
          backgroundColor: selected ? colors.accentMuted : pressed ? colors.surface2 : colors.elevatedSurface,
          opacity: disabled ? 0.5 : 1,
          transform: [{ scale: pressed ? PRESSED_SCALE : 1 }],
        },
      ]}
    >
      {icon ? <Ionicons name={icon} size={iconSize.sm} color={foreground} /> : null}
      <AppText variant="caption" numberOfLines={1} style={[styles.label, { color: selected ? colors.accentText : colors.textPrimary }]}>
        {label}
      </AppText>
      {trailingIcon ? <Ionicons name={trailingIcon} size={iconSize.sm} color={foreground} /> : null}
    </Pressable>
  )
}

const styles = StyleSheet.create({
  chip: {
    minHeight: 36,
    maxWidth: 260,
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.xs,
    paddingHorizontal: space.md,
    borderRadius: radius.full,
    borderWidth: StyleSheet.hairlineWidth,
  },
  label: { flexShrink: 1 },
})
