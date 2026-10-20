import { useRef, useState } from 'react'
import { StyleSheet, View, type TextInput } from 'react-native'

import { AuthShell } from '@/components/auth/AuthShell'
import { GoogleSignIn } from '@/components/auth/GoogleSignIn'
import { Button, Notice, TextField, TextLink } from '@/components/ui'
import { useT } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import { useAuthStore } from '@/stores/auth'

export default function RegisterScreen() {
  const t = useT()
  const [fullName, setFullName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const emailRef = useRef<TextInput>(null)
  const passwordRef = useRef<TextInput>(null)

  async function submit() {
    if (busy) return
    if (!email.trim() || !password) {
      setError(t('auth.register.missingFields'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      await useAuthStore.getState().signUp(email.trim(), password, fullName.trim())
    } catch (cause) {
      setError(userMessage(cause))
      setBusy(false)
    }
  }

  return (
    <AuthShell title={t('auth.register.title')} subtitle={t('auth.register.subtitle')}>
      <TextField
        label={t('auth.name')}
        value={fullName}
        onChangeText={setFullName}
        autoComplete="name"
        textContentType="name"
        returnKeyType="next"
        onSubmitEditing={() => emailRef.current?.focus()}
      />
      <TextField
        ref={emailRef}
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
        autoComplete="new-password"
        textContentType="newPassword"
        hint={t('auth.register.passwordHint')}
        returnKeyType="go"
        onSubmitEditing={submit}
      />
      {error ? <Notice tone="error">{error}</Notice> : null}
      <Button label={t('auth.register.submit')} size="lg" fullWidth loading={busy} onPress={submit} />
      <GoogleSignIn />
      <View style={styles.links}>
        <TextLink label={t('auth.register.haveAccount')} href="/login" />
      </View>
    </AuthShell>
  )
}

const styles = StyleSheet.create({
  links: { flexDirection: 'row', justifyContent: 'center' },
})
