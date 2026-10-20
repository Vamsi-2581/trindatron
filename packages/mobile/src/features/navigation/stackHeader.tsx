import { Stack } from 'expo-router'
import { useMemo, type ComponentProps } from 'react'
import { SafeAreaView } from 'react-native-safe-area-context'

import { AppHeader } from '@/components/ui'
import { useTheme } from '@/theme'

type StackOptions = Exclude<ComponentProps<typeof Stack>['screenOptions'], ((...args: never[]) => unknown) | undefined>

/**
 * Screen options every stack in the app shares: the native header is replaced by AppHeader, so a screen pushed
 * anywhere has the same back, the same title position and its `headerRight` in the same place. A screen keeps
 * naming itself the usual way (`options={{ title }}` in a layout or `<Stack.Screen options>` inside it).
 */
export function useStackScreenOptions(): StackOptions {
  const { colors } = useTheme()
  return useMemo<StackOptions>(
    () => ({
      contentStyle: { backgroundColor: colors.bg },
      header: ({ options }) => (
        <SafeAreaView edges={['top', 'left', 'right']} style={{ backgroundColor: colors.bg }}>
          <AppHeader
            title={typeof options.title === 'string' ? options.title : undefined}
            trailing={options.headerRight?.({ tintColor: colors.textPrimary, canGoBack: true })}
          />
        </SafeAreaView>
      ),
    }),
    [colors],
  )
}
