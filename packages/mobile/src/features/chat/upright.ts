import { Platform, type ViewStyle } from 'react-native'

/**
 * Turns a component handed to the inverted message list the right way up.
 *
 * The list is inverted so the newest message sits at the bottom, and React Native inverts it with a transform:
 * `scale: -1` on Android (both axes) and `scaleY: -1` everywhere else — see `verticallyInverted` in
 * @react-native/virtualized-lists. A FlatList only un-inverts its *cells*; a component passed for the empty, loading
 * or error state keeps the transform, so it has to be undone with the same one. Undoing only Y left every one of
 * those states mirrored left-to-right on Android: "Carregando conversa…" read backwards on a physical device.
 */
export function uprightStyle(platform: typeof Platform.OS = Platform.OS): ViewStyle {
  return platform === 'android' ? { transform: [{ scale: -1 }] } : { transform: [{ scaleY: -1 }] }
}
