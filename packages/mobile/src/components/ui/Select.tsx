import { Picker } from '@expo/ui/community/picker'
import Ionicons from '@expo/vector-icons/Ionicons'
import { useState } from 'react'
import { Platform, Pressable, StyleSheet } from 'react-native'

import { font, iconSize, radius, space, TOUCH_TARGET, useTheme } from '@/theme'

import { AppText } from './AppText'
import { BottomSheet, SheetOption } from './Sheet'

interface SelectProps<T extends string> {
  options: { value: T; label: string }[]
  value: T
  onChange: (value: T) => void
  disabled?: boolean
  accessibilityLabel?: string
}

/**
 * Pick one value from a list that is too long, or too wordy, for a segmented control (a language).
 *
 * iOS and the Web use @expo/ui's Picker (the system menu picker on iOS, a native <select> on the Web). Android does not:
 * its @expo/ui Picker is a Jetpack Compose host (`Host matchContents`) whose synchronous layout event, with Reanimated
 * listening, crashed a physical Android 16 device during navigation (expo/expo#47625). Android shows a field that
 * opens the options in the app's bottom sheet.
 */
export function Select<T extends string>(props: SelectProps<T>) {
  return Platform.OS === 'android' ? <SheetSelect {...props} /> : <ExpoSelect {...props} />
}

function SheetSelect<T extends string>({ options, value, onChange, disabled = false, accessibilityLabel }: SelectProps<T>) {
  const { colors } = useTheme()
  const [open, setOpen] = useState(false)
  const current = options.find((option) => option.value === value)
  return (
    <>
      <Pressable
        testID={accessibilityLabel}
        accessibilityRole="button"
        accessibilityLabel={accessibilityLabel ? `${accessibilityLabel}, ${current?.label ?? ''}` : current?.label}
        accessibilityState={{ disabled, expanded: open }}
        disabled={disabled}
        onPress={() => setOpen(true)}
        style={({ pressed }) => [
          styles.field,
          {
            backgroundColor: pressed ? colors.surface2 : colors.inputBackground,
            borderColor: colors.border,
            opacity: disabled ? 0.5 : 1,
          },
        ]}
      >
        <AppText variant="body" numberOfLines={1} style={styles.flex}>
          {current?.label ?? ''}
        </AppText>
        <Ionicons name="chevron-down" size={iconSize.sm} color={colors.textSecondary} />
      </Pressable>
      <BottomSheet visible={open} onClose={() => setOpen(false)} title={accessibilityLabel}>
        {options.map((option) => (
          <SheetOption
            key={option.value}
            label={option.label}
            selected={option.value === value}
            onPress={() => {
              setOpen(false)
              if (option.value !== value) onChange(option.value)
            }}
          />
        ))}
      </BottomSheet>
    </>
  )
}

function ExpoSelect<T extends string>({ options, value, onChange, disabled = false, accessibilityLabel }: SelectProps<T>) {
  const { colors } = useTheme()
  return (
    <Picker
      testID={accessibilityLabel}
      selectedValue={value}
      enabled={!disabled}
      onValueChange={(next) => {
        if (next !== value) onChange(next as T)
      }}
      // One flat object: on the Web the style goes straight onto a <select>, which cannot take an array.
      style={StyleSheet.flatten([styles.select, { backgroundColor: colors.inputBackground, borderColor: colors.border, color: colors.textPrimary }])}
    >
      {options.map((option) => (
        <Picker.Item key={option.value} label={option.label} value={option.value} color={colors.textPrimary} />
      ))}
    </Picker>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  field: {
    alignSelf: 'stretch',
    minHeight: TOUCH_TARGET,
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    paddingHorizontal: space.md,
    borderRadius: radius.md,
    borderWidth: StyleSheet.hairlineWidth,
  },
  select: {
    minHeight: 44,
    borderRadius: radius.md,
    borderWidth: StyleSheet.hairlineWidth,
    // Longhands and an explicit border style: the Web <select> reads CSS, not React Native shorthands.
    paddingLeft: space.md,
    paddingRight: space.md,
    borderStyle: 'solid',
    fontSize: font.size.md,
  } as object,
})
