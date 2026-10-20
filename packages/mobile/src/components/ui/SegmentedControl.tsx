import { SegmentedControl as NativeSegmentedControl } from '@expo/ui/community/segmented-control'
import { Platform, Pressable, StyleSheet, View } from 'react-native'

import { font, radius, space, TOUCH_TARGET, useTheme } from '@/theme'

import { AppText } from './AppText'

interface SegmentedControlProps<T extends string> {
  options: { value: T; label: string }[]
  value: T | null
  onChange: (value: T) => void
  disabled?: boolean
  accessibilityLabel?: string
}

/**
 * A choice between a few mutually exclusive options, shown all at once.
 *
 * iOS and the Web use @expo/ui's control (the system control on iOS, a vendored copy of
 * @react-native-segmented-control on the Web, MIT). Android does not: its @expo/ui control is a Jetpack Compose host
 * (`Host matchContents`) whose synchronous layout event, with Reanimated listening, crashed a physical Android 16
 * device during navigation (expo/expo#47625). Android draws the same control in React Native.
 */
export function SegmentedControl<T extends string>(props: SegmentedControlProps<T>) {
  return Platform.OS === 'android' ? <Segments {...props} /> : <ExpoSegments {...props} />
}

function Segments<T extends string>({ options, value, onChange, disabled = false, accessibilityLabel }: SegmentedControlProps<T>) {
  const { colors } = useTheme()
  return (
    <View
      testID={accessibilityLabel}
      accessibilityRole="radiogroup"
      accessibilityLabel={accessibilityLabel}
      style={[styles.track, { backgroundColor: colors.surface2, borderColor: colors.border, opacity: disabled ? 0.5 : 1 }]}
    >
      {options.map((option) => {
        const selected = option.value === value
        return (
          <Pressable
            key={option.value}
            accessibilityRole="radio"
            accessibilityLabel={option.label}
            accessibilityState={{ selected, checked: selected, disabled }}
            disabled={disabled}
            onPress={() => {
              if (!selected) onChange(option.value)
            }}
            style={({ pressed }) => [
              styles.segment,
              { backgroundColor: selected ? colors.accentMuted : pressed ? colors.surface3 : 'transparent' },
            ]}
          >
            <AppText
              variant="caption"
              numberOfLines={1}
              style={[styles.label, { color: selected ? colors.accentText : colors.textSecondary }]}
            >
              {option.label}
            </AppText>
          </Pressable>
        )
      })}
    </View>
  )
}

function ExpoSegments<T extends string>({ options, value, onChange, disabled = false, accessibilityLabel }: SegmentedControlProps<T>) {
  const { colors, scheme } = useTheme()
  const selectedIndex = Math.max(
    -1,
    options.findIndex((option) => option.value === value),
  )
  return (
    <NativeSegmentedControl
      testID={accessibilityLabel}
      values={options.map((option) => option.label)}
      selectedIndex={selectedIndex}
      enabled={!disabled}
      tintColor={colors.accent}
      appearance={scheme}
      onChange={(event) => {
        const option = options[event.nativeEvent.selectedSegmentIndex]
        if (option && option.value !== value) onChange(option.value)
      }}
      style={styles.control}
    />
  )
}

const styles = StyleSheet.create({
  control: { alignSelf: 'stretch', minHeight: 36 },
  track: {
    alignSelf: 'stretch',
    flexDirection: 'row',
    padding: 3,
    gap: 3,
    borderRadius: radius.full,
    borderWidth: StyleSheet.hairlineWidth,
  },
  segment: {
    flex: 1,
    minHeight: TOUCH_TARGET - 6,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: space.sm,
    borderRadius: radius.full,
  },
  label: { fontSize: font.size.sm, fontWeight: '600' },
})
