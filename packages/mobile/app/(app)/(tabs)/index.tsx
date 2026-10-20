import { router, useFocusEffect } from 'expo-router'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { StyleSheet, View } from 'react-native'
import { KeyboardAvoidingView, useKeyboardState } from 'react-native-keyboard-controller'
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context'

import { AppHeader, AppText, BrandLockup, Chip, CONTENT_MAX_WIDTH, IconButton, OfflineBanner } from '@/components/ui'
import { useCapabilityStore } from '@/features/capabilities/store'
import { StartConversation } from '@/features/chat/StartConversation'
import { startSuggestions, type Suggestion } from '@/features/chat/suggestions'
import { useConversationDrawer } from '@/features/navigation/ConversationDrawer'
import { useIsOffline } from '@/hooks/useNetworkStatus'
import { useT } from '@/i18n'
import { markBoot } from '@/services/bootTimeline'
import { useAuthStore } from '@/stores/auth'
import { space, useTheme } from '@/theme'
import { firstName } from '@/utils/format'

/**
 * Home is a new conversation: the menu, a greeting, a few ways to start that this account can really use, and the
 * same composer the conversation itself has. Conversations, tools and the account live in the side menu.
 */
export default function HomeScreen() {
  const { colors } = useTheme()
  const t = useT()
  const insets = useSafeAreaInsets()
  const keyboardVisible = useKeyboardState((state) => state.isVisible)
  const offline = useIsOffline()
  const user = useAuthStore((state) => state.user)
  const capabilities = useCapabilityStore((state) => state.capabilities)
  const drawer = useConversationDrawer()
  const { setEdgeSwipe } = drawer
  const userId = user?.id ?? null
  const [draft, setDraft] = useState<{ text: string; nonce: number } | null>(null)

  useFocusEffect(
    useCallback(() => {
      // The composer offers only what this account can really do; coming back refreshes it.
      void useCapabilityStore.getState().load(userId)
      setEdgeSwipe(true)
      return () => setEdgeSwipe(false)
    }, [userId, setEdgeSwipe]),
  )

  useEffect(() => {
    markBoot('homeRender')
  }, [])

  const suggestions = useMemo(() => startSuggestions(capabilities), [capabilities])
  const name = firstName(user?.full_name)

  function startFrom(suggestion: Suggestion) {
    if (suggestion.kind === 'create') {
      router.push({ pathname: '/create', params: { kind: suggestion.create } })
      return
    }
    // Starts the message, never sends it: the person finishes the thought.
    setDraft((previous) => ({ text: t(suggestion.promptKey), nonce: (previous?.nonce ?? 0) + 1 }))
  }

  return (
    <KeyboardAvoidingView behavior="padding" style={[styles.flex, { backgroundColor: colors.bg }]}>
      <SafeAreaView edges={['top', 'left', 'right']} style={styles.flex}>
        <AppHeader
          border={false}
          leading={drawer.docked ? 'none' : <IconButton icon="menu" label={t('drawer.open')} onPress={drawer.open} />}
          title={<BrandLockup size="sm" />}
        />
        {offline ? <OfflineBanner /> : null}

        <View style={styles.hero}>
          <View style={styles.greeting}>
            <AppText variant="screenTitle" accessibilityRole="header" style={styles.center}>
              {name ? t('home.greeting', { name }) : t('home.greetingAnonymous')}
            </AppText>
            <AppText tone="secondary" style={styles.center}>
              {t('home.subtitle')}
            </AppText>
          </View>
          <View style={styles.suggestions}>
            {suggestions.map((suggestion) => (
              <Chip
                key={suggestion.key}
                role="button"
                icon={suggestion.icon}
                label={t(suggestion.labelKey)}
                onPress={() => startFrom(suggestion)}
              />
            ))}
          </View>
        </View>

        <StartConversation variant="docked" bottomInset={keyboardVisible ? 0 : insets.bottom} draft={draft} />
      </SafeAreaView>
    </KeyboardAvoidingView>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  center: { textAlign: 'center' },
  hero: {
    flex: 1,
    justifyContent: 'center',
    gap: space.xl,
    paddingHorizontal: space.xl,
    width: '100%',
    maxWidth: CONTENT_MAX_WIDTH,
    alignSelf: 'center',
  },
  greeting: { gap: space.sm },
  suggestions: { flexDirection: 'row', flexWrap: 'wrap', justifyContent: 'center', gap: space.sm },
})
