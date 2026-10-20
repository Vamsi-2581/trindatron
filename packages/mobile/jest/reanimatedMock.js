/**
 * Reanimated under Jest.
 *
 * Reanimated 4 ships `mock.js`, but it cannot be used: it requires `src/mock.ts`, whose first imports
 * come from `src/index.ts` — the real module — which initialises react-native-worklets, whose native
 * side does not exist in Jest ("Cannot read properties of undefined (reading 'loadUnpackers')").
 * `react-native-worklets/jest` holds only a Babel-plugin helper and a resolver, no runtime mock.
 *
 * So the surface the chat library actually uses stands in here: animations resolve instantly to their
 * target value, animated styles are computed once, and worklets run on the JS thread. Nothing asserts
 * on animation behaviour — these exist so components that animate can be rendered and exercised.
 */
const React = require('react')
const { FlatList, Image, ScrollView, Text, View } = require('react-native')

/** Animations settle immediately: the target value is the final value, and the callback runs as if done. */
const settle = (toValue, _config, callback) => {
  if (typeof callback === 'function') callback(true, toValue)
  return toValue
}

const easing = (t) => t
easing.factory = () => easing
const constant = () => easing

const Easing = {
  linear: easing,
  ease: easing,
  quad: easing,
  cubic: easing,
  sin: easing,
  exp: easing,
  circle: easing,
  bounce: easing,
  step0: easing,
  step1: easing,
  back: constant,
  elastic: constant,
  poly: constant,
  bezier: constant,
  in: constant,
  out: constant,
  inOut: constant,
}

const Animated = {
  View,
  Text,
  Image,
  ScrollView,
  FlatList,
  createAnimatedComponent: (Component) => Component,
}

module.exports = {
  __esModule: true,
  default: Animated,

  Easing,
  ReduceMotion: { System: 'system', Always: 'always', Never: 'never' },
  Extrapolation: { CLAMP: 'clamp', EXTEND: 'extend', IDENTITY: 'identity' },

  useSharedValue: (initial) => {
    const ref = React.useRef(null)
    if (ref.current === null) {
      const shared = {
        value: initial,
        get: () => shared.value,
        set: (next) => {
          shared.value = typeof next === 'function' ? next(shared.value) : next
        },
        modify: (modifier) => {
          shared.value = modifier ? modifier(shared.value) : shared.value
        },
        addListener: () => {},
        removeListener: () => {},
      }
      ref.current = shared
    }
    return ref.current
  },
  useAnimatedStyle: (factory) => factory(),
  useAnimatedProps: (factory) => factory(),
  // Derived values are read through `.value` like any other, so one is handed back computed.
  useDerivedValue: (factory) => {
    const value = factory()
    return { value, get: () => value, addListener: () => {}, removeListener: () => {} }
  },
  useAnimatedReaction: () => {},
  // Reanimated hands scroll worklets the native payload itself (`event.contentOffset`), not a synthetic event, so a
  // scroll fired from a test is unwrapped the same way before it reaches them.
  useAnimatedScrollHandler: (handlers) => (event) => {
    const payload = event?.nativeEvent ?? event
    return typeof handlers === 'function' ? handlers(payload) : handlers?.onScroll?.(payload)
  },

  withTiming: settle,
  withSpring: settle,
  withDecay: settle,
  withDelay: (_delay, animation) => animation,
  withSequence: (...animations) => animations[animations.length - 1],
  withRepeat: (animation) => animation,
  cancelAnimation: () => {},

  /** Real linear interpolation, clamped: cheaper to implement than to fake, and it keeps values sane. */
  interpolate: (value, input, output) => {
    if (!input?.length || !output?.length) return value
    if (value <= input[0]) return output[0]
    if (value >= input[input.length - 1]) return output[output.length - 1]
    for (let i = 1; i < input.length; i += 1) {
      if (value <= input[i]) {
        const span = input[i] - input[i - 1]
        const ratio = span === 0 ? 0 : (value - input[i - 1]) / span
        return output[i - 1] + ratio * (output[i] - output[i - 1])
      }
    }
    return output[output.length - 1]
  },

  // No worklet runtime here, so both directions are a plain call on the JS thread.
  runOnJS: (fn) => fn,
  runOnUI: (fn) => fn,

  // ── What react-native-gesture-handler reads off this module ──────────────────
  // It requires Reanimated in a try/catch and treats a missing module as fine, but a module that is
  // present and incomplete is fatal ("Reanimated?.useComposedEventHandler is not a function"), so the
  // whole of its `ReanimatedPackage` contract is answered here. Gestures do not fire under Jest; these
  // exist so a component holding a GestureDetector can mount.
  isSharedValue: (value) => typeof value === 'object' && value !== null && 'value' in value,
  useEvent: () => () => {},
  useComposedEventHandler: () => () => {},
  useHandler: () => ({ context: {}, doDependenciesDiffer: false, useWeb: false }),
  /**
   * Supplied rather than left undefined: gesture-handler's fallback for an older Reanimated requires
   * `react-native-reanimated/src/createAnimatedComponent/NativeEventsManager`, a subpath that goes
   * around this mock and loads the real source. That throw is caught and ignored, but it would still
   * leave a failed module in the registry.
   *
   * `setGestureState` is deliberately absent — gesture-handler installs its own when it is missing.
   */
  NativeEventsManager: class {
    attachEvents() {}
    detachEvents() {}
    updateEvents() {}
  },
}
