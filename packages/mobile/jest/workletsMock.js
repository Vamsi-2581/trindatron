/**
 * Worklets under Jest.
 *
 * `react-native-worklets/src/index.ts` calls `init()` at import time, before any export is read, and
 * that reaches for a native module Jest does not have ("Cannot read properties of undefined (reading
 * 'loadUnpackers')"). There is no import path into the package that avoids it, so the whole module is
 * replaced rather than patched.
 *
 * Nothing we depend on imports this directly — the chat library does not. It arrives through
 * react-native-gesture-handler's ReanimatedSwipeable, ReanimatedDrawerLayout and reanimatedWrapper,
 * which between them use exactly the three names below. Without a worklet runtime there is only one
 * thread, so scheduling in either direction is a plain call.
 */
module.exports = {
  __esModule: true,
  isWorkletRuntime: () => false,
  scheduleOnRN: (fn, ...args) => fn?.(...args),
  scheduleOnUI: (fn, ...args) => fn?.(...args),
}
