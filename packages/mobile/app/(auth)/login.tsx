import { useRef, useState } from 'react'
import { StyleSheet, View, type TextInput } from 'react-native'

import { AuthShell } from '@/components/auth/AuthShell'
import { GoogleSignIn } from '@/components/auth/GoogleSignIn'
import { Button, Notice, TextField, TextLink } from '@/components/ui'
import { useT } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import { useAuthStore } from '@/stores/auth'
import { space } from '@/theme'

export default function LoginScreen() {
  const t = useT()
  const notice = useAuthStore((state) => state.notice)
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const passwordRef = useRef<TextInput>(null)

  async function submit() {
    if (busy) return
    if (!email.trim() || !password) {
      setError(t('auth.login.missingFields'))
      return
    }
    setBusy(true)
    setError(null)
    useAuthStore.getState().clearNotice()
    try {
      await useAuthStore.getState().signIn(email.trim(), password)
    } catch (cause) {
      setError(userMessage(cause))
      setBusy(false)
    }
  }

  return (
    <AuthShell title={t('auth.login.title')} subtitle={t('auth.login.subtitle')}>
      {notice ? <Notice tone="info">{t(notice)}</Notice> : null}
      <TextField
        label={t('auth.email')}
        value={email}
        onChangeText={setEmail}
        autoCapitalize="none"
        autoCorrect={false}
        autoComplete="email"
        keyboardType="email-address"
        textContentType="emailAddress"
        returnKeyType="next"
        onSubmitEditing={() => passwordRef.current?.focus()}
      />
      <TextField
        ref={passwordRef}
        label={t('auth.password')}
        value={password}
        onChangeText={setPassword}
        secureToggle
        autoCapitalize="none"
        autoComplete="current-password"
        textContentType="password"
        returnKeyType="go"
        onSubmitEditing={submit}
      />
      {error ? <Notice tone="error">{error}</Notice> : null}
      <Button label={t('auth.login.submit')} size="lg" fullWidth loading={busy} onPress={submit} />
      <GoogleSignIn />
      <View style={styles.links}>
        <TextLink label={t('auth.login.forgot')} href="/forgot-password" />
        <TextLink label={t('auth.login.createAccount')} href="/register" />
      </View>
    </AuthShell>
  )
}

const styles = StyleSheet.create({
  links: { flexDirection: 'row', justifyContent: 'space-between', flexWrap: 'wrap', gap: space.sm },
})
