import { forwardRef, useState } from 'react'
import { Platform, StyleSheet, TextInput, View, type TextInputProps } from 'react-native'

import { useT } from '@/i18n'
import { font, radius, space, TOUCH_TARGET, useTheme } from '@/theme'

import { AppText } from './AppText'
import { IconButton } from './Button'

export interface TextFieldProps extends TextInputProps {
  label: string
  error?: string | null
  hint?: string | null
  /** Adds a show/hide control and starts masked. */
  secureToggle?: boolean
}

export const TextField = forwardRef<TextInput, TextFieldProps>(function TextField(
  { label, error, hint, secureToggle = false, style, onFocus, onBlur, multiline, ...rest },
  ref,
) {
  const { colors } = useTheme()
  const t = useT()
  const [focused, setFocused] = useState(false)
  const [masked, setMasked] = useState(true)

  return (
    <View style={styles.wrapper}>
      <AppText variant="caption" tone="secondary">
        {label}
      </AppText>
      <View
        style={[
          styles.box,
          multiline && styles.multiline,
          {
            backgroundColor: colors.surface1,
            borderColor: error ? colors.error : focused ? colors.accent : colors.border,
          },
        ]}
      >
        <TextInput
          ref={ref}
          accessibilityLabel={label}
          placeholderTextColor={colors.textMuted}
          selectionColor={colors.accent}
          multiline={multiline}
          secureTextEntry={secureToggle ? masked : rest.secureTextEntry}
          onFocus={(event) => {
            setFocused(true)
            onFocus?.(event)
          }}
          onBlur={(event) => {
            setFocused(false)
            onBlur?.(event)
          }}
          style={[styles.input, multiline && styles.inputMultiline, { color: colors.textPrimary }, webNoOutline, style]}
          {...rest}
        />
        {secureToggle ? (
          <IconButton
            icon={masked ? 'eye-outline' : 'eye-off-outline'}
            label={masked ? t('a11y.showPassword') : t('a11y.hidePassword')}
            onPress={() => setMasked((value) => !value)}
          />
        ) : null}
      </View>
      {error ? (
        <AppText variant="caption" tone="error" accessibilityLiveRegion="polite">
          {error}
        </AppText>
      ) : hint ? (
        <AppText variant="caption" tone="muted">
          {hint}
        </AppText>
      ) : null}
    </View>
  )
})

// The focus state is drawn by the border colour, so the browser's own outline is redundant.
const webNoOutline = Platform.OS === 'web' ? ({ outlineStyle: 'none' } as object) : null

const styles = StyleSheet.create({
  wrapper: { gap: 6 },
  box: {
    minHeight: TOUCH_TARGET + 4,
    borderWidth: 1,
    borderRadius: radius.md,
    flexDirection: 'row',
    alignItems: 'center',
    paddingLeft: space.md,
  },
  multiline: { alignItems: 'flex-start', paddingVertical: space.sm },
  input: { flex: 1, fontSize: font.size.md, paddingVertical: space.sm, paddingRight: space.md },
  inputMultiline: { minHeight: 96, textAlignVertical: 'top' },
})
