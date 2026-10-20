import type { AuthSessionResult } from 'expo-auth-session'
import * as Google from 'expo-auth-session/providers/google'
import * as WebBrowser from 'expo-web-browser'
import { useState } from 'react'
import { StyleSheet, View } from 'react-native'

import { AppText, Button, Notice } from '@/components/ui'
import { exchangeGoogleIdToken, googleClientIds, isGoogleSignInAvailable } from '@/features/auth/google'
import { useT } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { space, useTheme } from '@/theme'

WebBrowser.maybeCompleteAuthSession()

export type GoogleOutcome = 'signedIn' | 'cancelled' | 'failed'

/**
 * Turns the OAuth result into a TRINDATRON session. Cancelling is not an error.
 * `nonce` is the one this request sent to Google; the Core checks the token carries it.
 */
export async function completeGoogleSignIn(result: AuthSessionResult, nonce: string | null | undefined): Promise<GoogleOutcome> {
  if (result.type === 'cancel' || result.type === 'dismiss' || result.type === 'locked') return 'cancelled'
  if (result.type !== 'success') return 'failed'
  const idToken = result.params.id_token ?? result.authentication?.idToken
  if (!idToken || !nonce) return 'failed'
  try {
    const session = await exchangeGoogleIdToken(idToken, nonce)
    await useAuthStore.getState().signInWithToken(session.access_token, session.refresh_token ?? null)
    return 'signedIn'
  } catch {
    return 'failed'
  }
}

/** Renders nothing until both the Core contract and the OAuth client exist. */
export function GoogleSignIn() {
  if (!isGoogleSignInAvailable()) return null
  return <GoogleSignInButton />
}

function GoogleSignInButton() {
  const t = useT()
  const { colors } = useTheme()
  const ids = googleClientIds()
  const [request, , promptAsync] = Google.useIdTokenAuthRequest({
    androidClientId: ids.android ?? undefined,
    iosClientId: ids.ios ?? undefined,
    webClientId: ids.web ?? undefined,
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function start() {
    setError(null)
    setBusy(true)
    const result = await promptAsync()
    const outcome = await completeGoogleSignIn(result, (request as { nonce?: string } | null)?.nonce)
    setBusy(false)
    if (outcome === 'failed') setError(t('auth.google.failed'))
  }

  return (
    <View style={styles.wrapper}>
      <View style={styles.dividerRow} accessible={false}>
        <View style={[styles.line, { backgroundColor: colors.border }]} />
        <AppText variant="caption" tone="muted">
          {t('auth.or')}
        </AppText>
        <View style={[styles.line, { backgroundColor: colors.border }]} />
      </View>
      <Button
        label={t('auth.google.continue')}
        variant="secondary"
        icon="logo-google"
        size="lg"
        fullWidth
        disabled={!request}
        loading={busy}
        onPress={start}
      />
      {error ? <Notice tone="error">{error}</Notice> : null}
    </View>
  )
}

const styles = StyleSheet.create({
  wrapper: { gap: space.md },
  dividerRow: { flexDirection: 'row', alignItems: 'center', gap: space.md },
  line: { flex: 1, height: StyleSheet.hairlineWidth },
})
