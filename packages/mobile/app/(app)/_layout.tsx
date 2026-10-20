import { Stack } from 'expo-router'
import { useEffect } from 'react'

import { useCapabilityStore } from '@/features/capabilities/store'
import { ConversationDrawerProvider } from '@/features/navigation/ConversationDrawer'
import { useStackScreenOptions } from '@/features/navigation/stackHeader'
import { useT } from '@/i18n'
import { useAuthStore } from '@/stores/auth'

export const unstable_settings = { initialRouteName: '(tabs)' }

export default function AppLayout() {
  const t = useT()
  const screenOptions = useStackScreenOptions()
  const userId = useAuthStore((state) => state.user?.id ?? null)

  // What this account can do is read once for the whole app, not by the first screen that happens to ask. A link
  // straight into Computer Use used to render before anyone loaded it and showed a plan limit as an outage.
  useEffect(() => {
    if (userId) void useCapabilityStore.getState().load(userId)
  }, [userId])

  return (
    // Conversations live in the side space (drawer on phones, docked panel on wide screens), not in a tab.
    <ConversationDrawerProvider>
      <Stack screenOptions={screenOptions}>
        <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
        {/* The chat places the same AppHeader itself, above its own keyboard-aware layout. */}
        <Stack.Screen name="chat/[sessionId]" options={{ headerShown: false }} />
        <Stack.Screen name="project/[projectId]" options={{ title: t('nav.project') }} />
        <Stack.Screen name="job/[jobId]" options={{ title: t('nav.job') }} />
        <Stack.Screen name="usage" options={{ title: t('nav.usage') }} />
        <Stack.Screen name="plans" options={{ title: t('nav.plans') }} />
        <Stack.Screen name="billing" options={{ title: t('nav.billing') }} />
        <Stack.Screen name="create" options={{ title: t('composer.create.image') }} />
        <Stack.Screen name="creations" options={{ title: t('creations.title') }} />
        <Stack.Screen name="creation/[jobId]" options={{ title: t('media.viewerTitle') }} />
        <Stack.Screen name="scheduled" options={{ title: t('nav.scheduled') }} />
        <Stack.Screen name="computer-use" options={{ title: t('nav.computerUse') }} />
        <Stack.Screen name="settings/index" options={{ title: t('nav.settings') }} />
        <Stack.Screen name="settings/devices" options={{ title: t('nav.devices') }} />
        <Stack.Screen name="settings/memories" options={{ title: t('nav.memories') }} />
        <Stack.Screen name="settings/privacy" options={{ title: t('nav.privacy') }} />
        <Stack.Screen name="team" options={{ title: t('nav.team') }} />
        <Stack.Screen name="support/index" options={{ title: t('nav.support') }} />
        <Stack.Screen name="support/[ticketId]" options={{ title: t('nav.ticket') }} />
      </Stack>
    </ConversationDrawerProvider>
  )
}
