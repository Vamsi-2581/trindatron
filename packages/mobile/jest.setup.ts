import { ReadableStream } from 'node:stream/web'
import { TextDecoder, TextEncoder } from 'node:util'

// Streaming primitives Expo provides at runtime; the Jest environment may not expose them.
Object.assign(globalThis, {
  ReadableStream: globalThis.ReadableStream ?? ReadableStream,
  TextEncoder: globalThis.TextEncoder ?? TextEncoder,
  TextDecoder: globalThis.TextDecoder ?? TextDecoder,
})

jest.mock('@react-native-async-storage/async-storage', () =>
  require('@react-native-async-storage/async-storage/jest/async-storage-mock'),
)
jest.mock('@react-native-community/netinfo', () => require('@react-native-community/netinfo/jest/netinfo-mock.js'))
jest.mock('react-native-keyboard-controller', () => {
  // The library ships a Jest stand-in for its components; its Reanimated hooks are not in it, and the composer reads
  // the keyboard's progress to release the bottom inset in step with the animation.
  const controller = require('react-native-keyboard-controller/jest')
  const { useSharedValue } = require('react-native-reanimated')
  return {
    ...controller,
    useReanimatedKeyboardAnimation: () => ({ height: useSharedValue(0), progress: useSharedValue(0) }),
  }
})
jest.mock('expo/fetch', () => ({ fetch: jest.fn() }))
// Deterministic language for every test; i18n tests override it explicitly.
jest.mock('expo-localization', () => ({
  getLocales: jest.fn(() => [{ languageTag: 'pt-BR', languageCode: 'pt', regionCode: 'BR' }]),
}))
// Icon fonts (expo-font) and bundled PNGs (expo-image) resolve through Metro's asset registry, which Jest does not have.
jest.mock('@expo/vector-icons/Ionicons', () => {
  const Icon = () => null
  Icon.glyphMap = {}
  return { __esModule: true, default: Icon }
})
// The opening animation finishes at once in tests; src/__tests__/brandIntro.test.tsx exercises the real one.
jest.mock('@/components/brand/BrandIntro', () => {
  const { useEffect } = jest.requireActual('react')
  return {
    INTRO_MAX_MS: 3200,
    BrandIntro: ({ onDone }: { onDone: () => void }) => {
      useEffect(() => onDone(), [onDone])
      return null
    },
  }
})
// Native file, share and video modules: tests that exercise them mock the calls they expect.
jest.mock('expo-sharing', () => ({ isAvailableAsync: jest.fn(async () => true), shareAsync: jest.fn(async () => undefined) }))
jest.mock('expo-file-system', () => ({
  File: class {
    static downloadFileAsync = jest.fn()
  },
  Directory: class {
    static pickDirectoryAsync = jest.fn()
  },
  Paths: { cache: {} },
}))
// Reanimated drives the chat list and composer; under Jest its worklets native module is absent.
// Its own mock.js cannot stand in — it loads the real module — so ours does; see the file for why.
jest.mock('react-native-reanimated', () => require('./jest/reanimatedMock'))
// Gesture handler reaches for the worklets runtime directly, past Reanimated, so that goes too.
jest.mock('react-native-worklets', () => require('./jest/workletsMock'))
// Gesture handler does ship a working Jest setup; it stands in for the native side of its own module.
require('react-native-gesture-handler/jestSetup')
// @expo/ui renders through Compose and SwiftUI hosts Jest does not have; see the file for what each stand-in keeps.
jest.mock('@expo/ui/community/bottom-sheet', () => {
  const ui = require('./jest/expoUiMock')
  return { __esModule: true, default: ui.BottomSheet, BottomSheetView: ui.BottomSheetView }
})
jest.mock('@expo/ui/community/menu', () => {
  const { MenuView } = require('./jest/expoUiMock')
  return { __esModule: true, default: MenuView, MenuView }
})
jest.mock('@expo/ui/community/segmented-control', () => {
  const { SegmentedControl } = require('./jest/expoUiMock')
  return { __esModule: true, default: SegmentedControl, SegmentedControl }
})
jest.mock('@expo/ui/community/picker', () => {
  const { Picker } = require('./jest/expoUiMock')
  return { __esModule: true, default: Picker, Picker }
})
jest.mock('expo-video',() => ({ useVideoPlayer: jest.fn(() => ({})), VideoView: () => null }))
// Recording and playback share one native module; a test that exercises either overrides what it needs.
jest.mock('expo-audio', () => ({
  RecordingPresets: { HIGH_QUALITY: {} },
  requestRecordingPermissionsAsync: jest.fn(async () => ({ granted: true })),
  setAudioModeAsync: jest.fn(async () => undefined),
  useAudioRecorder: jest.fn(() => ({
    prepareToRecordAsync: jest.fn(async () => undefined),
    record: jest.fn(),
    stop: jest.fn(async () => undefined),
    uri: null,
  })),
  useAudioRecorderState: jest.fn(() => ({ isRecording: false, durationMillis: 0 })),
  createAudioPlayer: jest.fn(() => ({
    play: jest.fn(),
    pause: jest.fn(),
    remove: jest.fn(),
    seekTo: jest.fn(async () => undefined),
  })),
}))
jest.mock('expo-image', () => {
  const { Image } = jest.requireActual('react-native')
  return { Image }
})
