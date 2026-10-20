import { Pressable, StyleSheet, View } from 'react-native'

import { radius, TOUCH_TARGET, useTheme } from '@/theme'

import { AppText } from './AppText'

/**
 * A switch that says what it does. The label and the explanation are part of
 * the control, because a row of unlabelled switches is a puzzle, and a setting
 * nobody understands is a setting nobody trusts.
 */
export function Toggle({
  label,
  description,
  value,
  onValueChange,
  disabled = false,
}: {
  label: string
  description?: string | null
  value: boolean
  onValueChange: (next: boolean) => void
  disabled?: boolean
}) {
  const { colors } = useTheme()
  return (
    <Pressable
      accessibilityRole="switch"
      accessibilityState={{ checked: value, disabled }}
      accessibilityLabel={label}
      accessibilityHint={description ?? undefined}
      disabled={disabled}
      onPress={() => onValueChange(!value)}
      style={({ pressed }) => [styles.row, { opacity: disabled ? 0.5 : pressed ? 0.85 : 1 }]}
    >
      <View style={styles.text}>
        <AppText>{label}</AppText>
        {description ? (
          <AppText variant="caption" tone="secondary">
            {description}
          </AppText>
        ) : null}
      </View>
      <View style={[styles.track, { backgroundColor: value ? colors.accent : colors.surface2, borderColor: value ? colors.accent : colors.border }]}>
        <View style={[styles.thumb, { backgroundColor: value ? colors.onAccent : colors.textMuted, alignSelf: value ? 'flex-end' : 'flex-start' }]} />
      </View>
    </Pressable>
  )
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', alignItems: 'center', gap: 12, minHeight: TOUCH_TARGET, paddingVertical: 8 },
  text: { flex: 1, gap: 2 },
  track: { width: 46, height: 28, borderRadius: radius.full, borderWidth: StyleSheet.hairlineWidth, padding: 3, justifyContent: 'center' },
  thumb: { width: 20, height: 20, borderRadius: 10 },
})
