import { Image } from 'expo-image'
import { useCallback, useEffect, useRef, useState } from 'react'
import { AccessibilityInfo, Animated, Easing, StyleSheet, useWindowDimensions, View } from 'react-native'

import { BRAND_ASSETS, SYMBOL_RATIO, WORDMARK_RATIO, wordmarkTint } from '@/components/ui/Brand'
import { palettes, space } from '@/theme'

/** Hard ceiling from mount: whatever happens (slow decoding, a stalled animation), the app is revealed by then. */
export const INTRO_MAX_MS = 4800
/** The longest the opening waits for its artwork to be decoded before it plays anyway. */
export const INTRO_ASSET_WAIT_MS = 900

type Layer = 'upper' | 'lower' | 'symbol' | 'glow' | 'wordmark'
const LAYER_COUNT = 5

const out = Easing.out(Easing.cubic)
const inOut = Easing.inOut(Easing.cubic)

function play(value: Animated.Value, duration: number, easing = out, delay = 0) {
  return Animated.timing(value, { toValue: 1, duration, delay, easing, useNativeDriver: true })
}

/**
 * Opening identity, built in six beats from the official artwork:
 *   1. the upper infinity enters;
 *   2. the lower loop enters, on its own;
 *   3. the two, still apart, converge and interlock;
 *   4. the untouched official symbol takes the frame;
 *   5. the cyan light rises, behind the knot and then around it;
 *   6. the wordmark settles.
 *
 * It plays only once its layers are decoded — before, the construction ran
 * behind the native splash and only the finished symbol was ever seen — and it
 * hands over from the native splash at that moment (`onReady`). It never blocks
 * startup, ignores touches, shows the finished frame when the system asks for
 * reduced motion, and ends at once if an asset cannot be drawn.
 */
export function BrandIntro({ onDone, onReady }: { onDone: () => void; onReady?: () => void }) {
  const { width } = useWindowDimensions()
  const symbolWidth = Math.round(Math.min(width * 0.46, 210))
  const symbolHeight = Math.round(symbolWidth / SYMBOL_RATIO)
  const wordmarkWidth = Math.round(symbolWidth * 1.05)

  const [beats] = useState(() => ({
    upper: new Animated.Value(0),
    lower: new Animated.Value(0),
    join: new Animated.Value(0),
    lock: new Animated.Value(0),
    light: new Animated.Value(0),
    word: new Animated.Value(0),
    exit: new Animated.Value(0),
  }))
  const [motion, setMotion] = useState<'unknown' | 'full' | 'reduced'>('unknown')
  const [artworkReady, setArtworkReady] = useState(false)
  const loaded = useRef(new Set<Layer>())
  const animation = useRef<Animated.CompositeAnimation | null>(null)

  // The parent re-renders while the session boots; the animation must not restart with it.
  const callbacks = useRef({ onDone, onReady })
  useEffect(() => {
    callbacks.current = { onDone, onReady }
  })

  const finished = useRef(false)
  const finish = useCallback(() => {
    if (finished.current) return
    finished.current = true
    callbacks.current.onDone()
  }, [])

  const onLoad = useCallback((layer: Layer) => {
    loaded.current.add(layer)
    if (loaded.current.size === LAYER_COUNT) setArtworkReady(true)
  }, [])

  useEffect(() => {
    const ceiling = setTimeout(finish, INTRO_MAX_MS)
    const wait = setTimeout(() => setArtworkReady(true), INTRO_ASSET_WAIT_MS)
    let cancelled = false
    Promise.resolve(AccessibilityInfo.isReduceMotionEnabled())
      .catch(() => false)
      .then((reduced) => {
        if (!cancelled) setMotion(reduced ? 'reduced' : 'full')
      })
    return () => {
      cancelled = true
      clearTimeout(ceiling)
      clearTimeout(wait)
    }
  }, [finish])

  useEffect(() => () => animation.current?.stop(), [])

  useEffect(() => {
    if (animation.current || motion === 'unknown' || (motion === 'full' && !artworkReady)) return
    const { upper, lower, join, lock, light, word, exit } = beats
    let sequence: Animated.CompositeAnimation
    if (motion === 'reduced') {
      for (const value of [upper, lower, join, lock, light, word]) value.setValue(1)
      sequence = Animated.sequence([Animated.delay(600), play(exit, 220)])
    } else {
      sequence = Animated.sequence([
        play(upper, 520),
        play(lower, 520),
        Animated.delay(60),
        play(join, 460, inOut),
        play(lock, 180),
        Animated.parallel([play(light, 820, inOut), play(word, 420, out, 420)]),
        Animated.delay(320),
        play(exit, 280),
      ])
    }
    animation.current = sequence
    callbacks.current.onReady?.()
    sequence.start(({ finished: completed }) => {
      if (completed) finish()
    })
  }, [motion, artworkReady, beats, finish])

  const { upper, lower, join, lock, light, word, exit } = beats
  /** A layer's offset: where it enters from (`from`), plus how far apart it waits before the join (`apart`). */
  const along = (enter: Animated.Value, from: number, apart: number) =>
    Animated.add(
      enter.interpolate({ inputRange: [0, 1], outputRange: [from, 0] }),
      join.interpolate({ inputRange: [0, 1], outputRange: [apart, 0] }),
    )
  const layer = { width: symbolWidth, height: symbolHeight }
  const halo = symbolWidth * 1.8
  const core = symbolWidth * 0.8

  return (
    <Animated.View
      testID="brand-intro"
      pointerEvents="none"
      accessible
      accessibilityLabel="TRINDATRON"
      style={[styles.root, { opacity: exit.interpolate({ inputRange: [0, 1], outputRange: [1, 0] }) }]}
    >
      <View style={layer}>
        {/* 5 — light behind the symbol */}
        <Animated.View
          style={[
            styles.abs,
            {
              width: halo,
              height: halo,
              left: (symbolWidth - halo) / 2,
              top: (symbolHeight - halo) / 2,
              opacity: light.interpolate({ inputRange: [0, 1], outputRange: [0, 0.55] }),
              transform: [{ scale: light.interpolate({ inputRange: [0, 1], outputRange: [0.55, 1] }) }],
            },
          ]}
        >
          <Image source={BRAND_ASSETS.glow} style={StyleSheet.absoluteFill} contentFit="fill" onLoad={() => onLoad('glow')} onError={finish} accessible={false} />
        </Animated.View>
        <Animated.View
          style={[
            styles.abs,
            {
              width: core,
              height: core,
              left: (symbolWidth - core) / 2,
              top: (symbolHeight - core) / 2,
              opacity: light.interpolate({ inputRange: [0, 0.6, 1], outputRange: [0, 0.7, 0.45] }),
              transform: [{ scale: light.interpolate({ inputRange: [0, 1], outputRange: [0.3, 1] }) }],
            },
          ]}
        >
          <Image source={BRAND_ASSETS.glow} style={StyleSheet.absoluteFill} contentFit="fill" onLoad={() => onLoad('glow')} onError={finish} accessible={false} />
        </Animated.View>

        {/* 1 — the upper infinity */}
        <Animated.View
          style={[
            styles.abs,
            layer,
            {
              opacity: upper.interpolate({ inputRange: [0, 0.25, 1], outputRange: [0, 1, 1] }),
              transform: [
                { translateX: along(upper, -symbolWidth * 0.7, -symbolWidth * 0.09) },
                { translateY: along(upper, -symbolHeight * 0.45, -symbolHeight * 0.1) },
                { rotate: upper.interpolate({ inputRange: [0, 1], outputRange: ['-26deg', '0deg'] }) },
              ],
            },
          ]}
        >
          <Image source={BRAND_ASSETS.introUpper} style={StyleSheet.absoluteFill} contentFit="contain" onLoad={() => onLoad('upper')} onError={finish} accessible={false} />
        </Animated.View>

        {/* 2 — the lower loop */}
        <Animated.View
          style={[
            styles.abs,
            layer,
            {
              opacity: lower.interpolate({ inputRange: [0, 0.25, 1], outputRange: [0, 1, 1] }),
              transform: [
                { translateX: along(lower, symbolWidth * 0.55, symbolWidth * 0.07) },
                { translateY: along(lower, symbolHeight * 0.8, symbolHeight * 0.14) },
                { rotate: lower.interpolate({ inputRange: [0, 1], outputRange: ['30deg', '0deg'] }) },
              ],
            },
          ]}
        >
          <Image source={BRAND_ASSETS.introLower} style={StyleSheet.absoluteFill} contentFit="contain" onLoad={() => onLoad('lower')} onError={finish} accessible={false} />
        </Animated.View>

        {/* 3 → 4 — joined, the untouched official symbol takes the frame (the layers coincide with it pixel for pixel) */}
        <Animated.View style={[styles.abs, layer, { opacity: lock }]}>
          <Image source={BRAND_ASSETS.symbolHd} style={StyleSheet.absoluteFill} contentFit="contain" onLoad={() => onLoad('symbol')} onError={finish} accessible={false} />
        </Animated.View>
      </View>

      {/* 6 — the wordmark */}
      <Animated.View
        style={{
          marginTop: space.xl,
          opacity: word,
          transform: [{ translateY: word.interpolate({ inputRange: [0, 1], outputRange: [12, 0] }) }],
        }}
      >
        <Image
          source={BRAND_ASSETS.wordmark}
          tintColor={wordmarkTint('dark', palettes.dark.chromeHigh).tintColor}
          style={{ width: wordmarkWidth, height: Math.round(wordmarkWidth / WORDMARK_RATIO) }}
          contentFit="contain"
          onLoad={() => onLoad('wordmark')}
          onError={finish}
          accessible={false}
        />
      </Animated.View>
    </Animated.View>
  )
}

const styles = StyleSheet.create({
  // Always the dark brand ground, like the native splash it continues.
  root: {
    position: 'absolute',
    top: 0,
    right: 0,
    bottom: 0,
    left: 0,
    backgroundColor: palettes.dark.bg,
    alignItems: 'center',
    justifyContent: 'center',
  },
  abs: { position: 'absolute', left: 0, top: 0 },
})
