import { Linking, StyleSheet, View } from 'react-native'

import { AuthShell } from '@/components/auth/AuthShell'
import { Button, Notice, TextLink } from '@/components/ui'
import { useT } from '@/i18n'

/**
 * BLOCKED BY CORE: TRINITY CORE has no password-reset endpoint (only
 * /auth/register, /auth/login, /auth/me). This screen says so instead of
 * pretending to send an e-mail. The support address is the one the Web
 * already uses for manual requests (packages/web/src/app/(app)/plans/page.tsx).
 */
const SUPPORT_EMAIL = 'contato@trinity.ai'

export default function ForgotPasswordScreen() {
  const t = useT()
  return (
    <AuthShell title={t('auth.forgot.title')}>
      <Notice tone="info">{t('auth.forgot.body')}</Notice>
      <Button
        label={t('common.contactSupport')}
        variant="secondary"
        icon="mail-outline"
        fullWidth
        onPress={() =>
          Linking.openURL(`mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent(t('auth.forgot.emailSubject'))}`).catch(() => undefined)
        }
      />
      <View style={styles.links}>
        <TextLink label={t('auth.forgot.back')} href="/login" />
      </View>
    </AuthShell>
  )
}

const styles = StyleSheet.create({
  links: { flexDirection: 'row', justifyContent: 'center' },
})
