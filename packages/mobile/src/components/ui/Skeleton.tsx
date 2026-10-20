import { useEffect } from 'react'
import { StyleSheet, View, type DimensionValue } from 'react-native'
import Animated, { cancelAnimation, useAnimatedStyle, useSharedValue, withRepeat, withTiming } from 'react-native-reanimated'

import { useT } from '@/i18n'
import { motion, radius, space, useTheme } from '@/theme'

/**
 * The shape of content that is on its way. It breathes on the UI thread (Reanimated, already in the app) instead of
 * spinning in the middle of an empty page, so a list opens in place instead of jumping when the data lands.
 */
export function Skeleton({ width = '100%', height = 14, round = false }: { width?: DimensionValue; height?: number; round?: boolean }) {
  const { colors } = useTheme()
  const opacity = useSharedValue(1)

  useEffect(() => {
    opacity.value = withRepeat(withTiming(0.45, { duration: motion.slow }), -1, true)
    return () => cancelAnimation(opacity)
  }, [opacity])

  const pulse = useAnimatedStyle(() => ({ opacity: opacity.value }))
  return (
    <Animated.View
      style={[{ width, height, borderRadius: round ? radius.full : radius.sm, backgroundColor: colors.surface3 }, pulse]}
    />
  )
}

/** A placeholder for a list of rows: a title line and a detail line each. Announced once as loading. */
export function SkeletonRows({ count = 4, icon = false }: { count?: number; icon?: boolean }) {
  const t = useT()
  return (
    <View accessible accessibilityRole="progressbar" accessibilityLabel={t('common.loading')} style={styles.rows}>
      {Array.from({ length: count }, (_, index) => (
        <View key={index} style={styles.row}>
          {icon ? <Skeleton width={20} height={20} round /> : null}
          <View style={styles.lines}>
            <Skeleton width={index % 2 === 0 ? '70%' : '55%'} height={14} />
            <Skeleton width="35%" height={10} />
          </View>
        </View>
      ))}
    </View>
  )
}

const styles = StyleSheet.create({
  rows: { gap: space.md, paddingVertical: space.sm },
  row: { flexDirection: 'row', alignItems: 'center', gap: space.md, paddingHorizontal: space.lg, minHeight: 44 },
  lines: { flex: 1, gap: space.xs },
})
