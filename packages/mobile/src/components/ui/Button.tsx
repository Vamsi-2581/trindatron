import Ionicons from '@expo/vector-icons/Ionicons'
import type { ComponentProps } from 'react'
import { ActivityIndicator, Pressable, StyleSheet, View, type PressableProps, type StyleProp, type ViewStyle } from 'react-native'

import { iconSize, radius, space, TOUCH_TARGET, useTheme } from '@/theme'

import { AppText } from './AppText'

export type IconName = ComponentProps<typeof Ionicons>['name']
export type IconSize = keyof typeof iconSize
export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'

/** A pressed control gives a little under the finger, the same way everywhere. */
export const PRESSED_SCALE = 0.97

export interface ButtonProps extends Omit<PressableProps, 'children' | 'style'> {
  label: string
  variant?: ButtonVariant
  icon?: IconName
  loading?: boolean
  size?: 'md' | 'lg'
  fullWidth?: boolean
  style?: StyleProp<ViewStyle>
}

export function Button({
  label,
  variant = 'primary',
  icon,
  loading = false,
  disabled,
  size = 'md',
  fullWidth = false,
  style,
  ...rest
}: ButtonProps) {
  const { colors } = useTheme()
  const isDisabled = Boolean(disabled) || loading
  const palette = {
    primary: { bg: colors.accent, pressed: colors.accentPressed, fg: colors.onAccent, border: colors.accent },
    secondary: { bg: colors.surface2, pressed: colors.surface3, fg: colors.textPrimary, border: colors.border },
    ghost: { bg: 'transparent', pressed: colors.surface2, fg: colors.accentText, border: 'transparent' },
    danger: { bg: colors.errorMuted, pressed: colors.surface3, fg: colors.error, border: 'transparent' },
  }[variant]
  // Disabled reads as disabled and stays legible: a neutral surface with muted text at full opacity. Fading the
  // whole button to half left white-on-accent text nearly invisible on the dark theme ("Criar team e pagar").
  const inactive = isDisabled && !loading
  const shown = inactive
    ? { ...palette, bg: variant === 'ghost' ? 'transparent' : colors.surface2, fg: colors.textMuted, border: variant === 'ghost' ? 'transparent' : colors.border }
    : palette

  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ disabled: isDisabled, busy: loading }}
      disabled={isDisabled}
      style={({ pressed }) => [
        styles.base,
        size === 'lg' && styles.large,
        fullWidth && styles.fullWidth,
        {
          backgroundColor: pressed ? shown.pressed : shown.bg,
          borderColor: shown.border,
          transform: [{ scale: pressed ? PRESSED_SCALE : 1 }],
        },
        style,
      ]}
      {...rest}
    >
      {loading ? (
        <ActivityIndicator color={shown.fg} />
      ) : (
        <View style={styles.row}>
          {icon ? <Ionicons name={icon} size={iconSize.sm} color={shown.fg} /> : null}
          <AppText variant="bodyStrong" numberOfLines={1} style={{ color: shown.fg }}>
            {label}
          </AppText>
        </View>
      )}
    </Pressable>
  )
}

export function IconButton({
  icon,
  label,
  onPress,
  disabled,
  tone = 'default',
  size = 'lg',
  style,
}: {
  icon: IconName
  label: string
  onPress?: () => void
  disabled?: boolean
  tone?: 'default' | 'accent' | 'danger' | 'onAccent'
  size?: IconSize
  style?: StyleProp<ViewStyle>
}) {
  const { colors } = useTheme()
  const color = { default: colors.textSecondary, accent: colors.accentText, danger: colors.error, onAccent: colors.onAccent }[tone]
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ disabled: Boolean(disabled) }}
      disabled={disabled}
      onPress={onPress}
      hitSlop={4}
      style={({ pressed }) => [
        styles.icon,
        {
          backgroundColor: pressed ? colors.surface2 : 'transparent',
          opacity: disabled ? 0.4 : 1,
          transform: [{ scale: pressed ? PRESSED_SCALE : 1 }],
        },
        style,
      ]}
    >
      <Ionicons name={icon} size={iconSize[size]} color={color} />
    </Pressable>
  )
}

const styles = StyleSheet.create({
  base: {
    minHeight: TOUCH_TARGET,
    paddingHorizontal: space.lg,
    borderRadius: radius.md,
    borderWidth: StyleSheet.hairlineWidth,
    alignItems: 'center',
    justifyContent: 'center',
  },
  large: { minHeight: 52, borderRadius: radius.lg },
  fullWidth: { alignSelf: 'stretch' },
  row: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  icon: {
    width: TOUCH_TARGET,
    height: TOUCH_TARGET,
    borderRadius: radius.md,
    alignItems: 'center',
    justifyContent: 'center',
  },
})
