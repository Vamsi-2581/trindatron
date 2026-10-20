import Constants from 'expo-constants'
import { DarkTheme, DefaultTheme, Stack, ThemeProvider } from 'expo-router'
import * as SplashScreen from 'expo-splash-screen'
import { StatusBar } from 'expo-status-bar'
import { useEffect, useState } from 'react'
import { AppState, StyleSheet, View } from 'react-native'
import { KeyboardProvider } from 'react-native-keyboard-controller'
import { SafeAreaProvider } from 'react-native-safe-area-context'

import { BrandIntro } from '@/components/brand/BrandIntro'
import { EmptyState, LoadingState } from '@/components/ui'
import { API_BASE_URL } from '@/config/env'
import { syncLanguageWithAccount } from '@/features/settings/language'
import { useLocaleStore, useT } from '@/i18n'
import { markBoot, setBootReporter } from '@/services/bootTimeline'
import { useAuthStore } from '@/stores/auth'
import { useTheme } from '@/theme'

SplashScreen.preventAutoHideAsync().catch(() => undefined)

// Development builds send their measured startup to the Core's development-only
// diagnostics route (never mounted in production), so it can be compared before and after a change.
if (__DEV__ && API_BASE_URL && process.env.NODE_ENV !== 'test') {
  setBootReporter(async (timeline) => {
    await fetch(`${API_BASE_URL}/diagnostics/client-timing`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...timeline, app_version: Constants.expoConfig?.version ?? null }),
    })
  })
}

/** The opening animation plays once per process, on cold start only. */
let introPlayed = false

function hideSplash() {
  SplashScreen.hideAsync().catch(() => undefined)
}

export default function RootLayout() {
  const { scheme, colors } = useTheme()
  const t = useT()
  const status = useAuthStore((state) => state.status)
  const localeReady = useLocaleStore((state) => state.hydrated)
  const [intro, setIntro] = useState(() => !introPlayed)

  useEffect(() => {
    void useLocaleStore.getState().hydrate()
    if (API_BASE_URL) void useAuthStore.getState().boot()
  }, [])

  useEffect(() => {
    if (!intro) markBoot('introDone')
  }, [intro])

  // Signed in: the app's language and the account's become one (features/settings/language.ts). Never blocks the app;
  // a failure leaves the phone's choice in place, and every message still carries it.
  useEffect(() => {
    if (status !== 'signedIn' || !API_BASE_URL) return
    const controller = new AbortController()
    syncLanguageWithAccount(controller.signal).catch(() => undefined)
    return () => controller.abort()
  }, [status])

  // Coming back to the app never signs anyone out. It only re-reads the OS
  // language and retries a secure-store read that failed while the phone was locked.
  useEffect(() => {
    const subscription = AppState.addEventListener('change', (next) => {
      if (next !== 'active') return
      useLocaleStore.getState().refreshSystemLocale()
      if (API_BASE_URL && useAuthStore.getState().storageUnavailable) void useAuthStore.getState().boot()
    })
    return () => subscription.remove()
  }, [])

  // While the opening plays, it takes over from the native splash the moment its
  // artwork is on screen (onReady); otherwise the splash goes once the app is ready.
  const ready = localeReady && (status !== 'booting' || !API_BASE_URL)
  useEffect(() => {
    if (ready && !intro) hideSplash()
  }, [ready, intro])

  const base = scheme === 'dark' ? DarkTheme : DefaultTheme
  const navigationTheme = {
    ...base,
    colors: {
      ...base.colors,
      primary: colors.accent,
      background: colors.bg,
      card: colors.surface1,
      text: colors.textPrimary,
      border: colors.border,
      notification: colors.accent,
    },
  }

  let body
  if (!API_BASE_URL) {
    body = (
      <View style={[styles.center, { backgroundColor: colors.bg }]}>
        <EmptyState icon="settings-outline" title={t('config.missingTitle')} body={t('config.missingBody')} />
      </View>
    )
  } else if (!ready) {
    body = (
      <View style={[styles.center, { backgroundColor: colors.bg }]}>
        <LoadingState label={t('boot.opening')} />
      </View>
    )
  } else {
    body = (
      <Stack screenOptions={{ headerShown: false, contentStyle: { backgroundColor: colors.bg } }}>
        <Stack.Protected guard={status === 'signedIn'}>
          <Stack.Screen name="(app)" />
        </Stack.Protected>
        <Stack.Protected guard={status !== 'signedIn'}>
          <Stack.Screen name="(auth)" />
        </Stack.Protected>
        <Stack.Screen name="+not-found" />
      </Stack>
    )
  }

  return (
    <SafeAreaProvider>
      <KeyboardProvider>
        <ThemeProvider value={navigationTheme}>
          <StatusBar style={intro || scheme === 'dark' ? 'light' : 'dark'} />
          {body}
          {intro ? (
            <BrandIntro
              onReady={hideSplash}
              onDone={() => {
                introPlayed = true
                setIntro(false)
              }}
            />
          ) : null}
        </ThemeProvider>
      </KeyboardProvider>
    </SafeAreaProvider>
  )
}

const styles = StyleSheet.create({
  center: { flex: 1, justifyContent: 'center' },
})
