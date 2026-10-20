import { Stack } from 'expo-router'

import { useStackScreenOptions } from '@/features/navigation/stackHeader'
import { useT } from '@/i18n'

export const unstable_settings = { initialRouteName: 'index' }

/**
 * No tab bar. Home is the conversation; Projects, Files, Agents and Profile open
 * from the side menu as ordinary screens with a way back to Home.
 */
export default function MainLayout() {
  const t = useT()
  const screenOptions = useStackScreenOptions()

  return (
    <Stack screenOptions={screenOptions}>
      <Stack.Screen name="index" options={{ headerShown: false }} />
      <Stack.Screen name="projects" options={{ title: t('nav.projects') }} />
      <Stack.Screen name="files" options={{ title: t('nav.files') }} />
      <Stack.Screen name="agents" options={{ title: t('nav.agents') }} />
      <Stack.Screen name="profile" options={{ title: t('nav.profile') }} />
    </Stack>
  )
}
