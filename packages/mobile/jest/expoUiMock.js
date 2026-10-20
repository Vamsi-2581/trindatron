/**
 * @expo/ui under Jest.
 *
 * Its community components render Jetpack Compose or SwiftUI through a native host that Jest does not have. These
 * stand-ins keep each one's contract — the props the app passes and the events it listens to — as plain React
 * Native views, so a screen that uses them can be rendered and driven the way a person would.
 */
const React = require('react')
const { Pressable, Text, View } = require('react-native')

/** Open while `index >= 0`; `close()`/`dismiss()` close it and report `onClose`, like the native sheet. */
const BottomSheet = React.forwardRef(function BottomSheet({ index = 0, onClose, onDismiss, children }, ref) {
  const [open, setOpen] = React.useState(index >= 0)
  const close = React.useCallback(() => {
    setOpen(false)
    onClose?.()
    onDismiss?.()
  }, [onClose, onDismiss])
  React.useImperativeHandle(ref, () => ({
    close,
    dismiss: close,
    forceClose: close,
    present: () => setOpen(true),
    expand: () => setOpen(true),
    collapse: () => undefined,
    snapToIndex: (next) => (next >= 0 ? setOpen(true) : close()),
    snapToPosition: () => undefined,
  }))
  return open ? React.createElement(View, { testID: 'bottom-sheet' }, children) : null
})

const BottomSheetView = ({ children, style }) => React.createElement(View, { style }, children)

/** The trigger stays in the tree; pressing it lists the actions as buttons named by their titles. */
function MenuView({ actions = [], onPressAction, children }) {
  const [open, setOpen] = React.useState(false)
  return React.createElement(
    View,
    null,
    React.createElement(Pressable, { onPress: () => setOpen((value) => !value) }, children),
    open
      ? actions.map((action) =>
          React.createElement(
            Pressable,
            {
              key: action.id ?? action.title,
              accessibilityRole: 'button',
              accessibilityLabel: action.title,
              onPress: () => {
                setOpen(false)
                onPressAction?.({ nativeEvent: { event: action.id ?? action.title } })
              },
            },
            React.createElement(Text, null, action.title),
          ),
        )
      : null,
  )
}

/** One radio per segment, named by its label. */
function SegmentedControl({ values = [], selectedIndex = -1, enabled = true, onChange, onValueChange, testID }) {
  return React.createElement(
    View,
    { testID, accessibilityRole: 'radiogroup' },
    values.map((value, index) =>
      React.createElement(
        Pressable,
        {
          key: value,
          accessibilityRole: 'radio',
          accessibilityLabel: value,
          accessibilityState: { selected: index === selectedIndex, disabled: !enabled },
          disabled: !enabled,
          onPress: () => {
            onChange?.({ nativeEvent: { selectedSegmentIndex: index, value } })
            onValueChange?.(value)
          },
        },
        React.createElement(Text, null, value),
      ),
    ),
  )
}

function PickerItem() {
  return null
}

/** One radio per item, named by its label, so a test can pick a value by what the person reads. */
function Picker({ selectedValue, onValueChange, enabled = true, testID, children, style }) {
  // The Web picker hands `style` to a DOM <select>, which throws on an array: the app must pass one object.
  if (Array.isArray(style)) throw new Error('Picker style must be a single object (the Web <select> cannot take an array)')
  const items = React.Children.toArray(children).map((child) => child.props)
  return React.createElement(
    View,
    { testID, accessibilityRole: 'radiogroup' },
    items.map((item, index) =>
      React.createElement(
        Pressable,
        {
          key: String(item.value),
          accessibilityRole: 'radio',
          accessibilityLabel: item.label,
          accessibilityState: { selected: item.value === selectedValue, disabled: !enabled },
          disabled: !enabled,
          onPress: () => onValueChange?.(item.value, index),
        },
        React.createElement(Text, null, item.label),
      ),
    ),
  )
}
Picker.Item = PickerItem

module.exports = { BottomSheet, BottomSheetView, MenuView, SegmentedControl, Picker }
