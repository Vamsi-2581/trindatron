import Constants from 'expo-constants'
import { router } from 'expo-router'
import { useState } from 'react'
import { StyleSheet, View } from 'react-native'

import { AppText, Badge, BottomSheet, Button, ListRow, Screen, Section, SheetOption, Surface } from '@/components/ui'
import { API_BASE_URL } from '@/config/env'
import { BILLING_HREF, PLANS_HREF, SETTINGS_HREF, SETTINGS_PRIVACY_HREF, SUPPORT_HREF, USAGE_HREF } from '@/features/navigation/routes'
import { chooseLanguage } from '@/features/settings/language'
import { useLocaleStore, useT, type LanguagePreference } from '@/i18n'
import { useAuthStore } from '@/stores/auth'
import { radius, space, useTheme } from '@/theme'
import { confirmAction } from '@/utils/confirm'
import { planLabel } from '@/utils/format'

const LANGUAGE_OPTIONS: LanguagePreference[] = ['auto', 'pt', 'en', 'es']

export default function ProfileScreen() {
  const { colors } = useTheme()
  const t = useT()
  const user = useAuthStore((state) => state.user)
  const preference = useLocaleStore((state) => state.preference)
  const systemLocale = useLocaleStore((state) => state.systemLocale)
  const [languageOpen, setLanguageOpen] = useState(false)
  const initial = (user?.full_name || user?.email || '?').trim().charAt(0).toUpperCase()

  const languageValue =
    preference === 'auto' ? t('profile.languageAutoDetail', { language: t(`languages.${systemLocale}`) }) : t(`languages.${preference}`)

  async function signOut() {
    const confirmed = await confirmAction({
      title: t('profile.signOutTitle'),
      message: t('profile.signOutMessage'),
      confirmLabel: t('profile.signOut'),
      destructive: true,
    })
    if (confirmed) await useAuthStore.getState().signOut()
  }

  return (
    <Screen>
      <View style={styles.identity}>
        <View style={[styles.avatar, { backgroundColor: colors.accentMuted }]} accessible={false}>
          <AppText variant="title" tone="accent">
            {initial}
          </AppText>
        </View>
        <View style={styles.flex}>
          <AppText variant="heading" numberOfLines={1}>
            {user?.full_name || t('profile.accountFallback')}
          </AppText>
          <AppText variant="caption" tone="secondary" numberOfLines={1}>
            {user?.email ?? '—'}
          </AppText>
          <Badge label={t('home.plan', { plan: planLabel(user?.plan) })} tone="accent" />
        </View>
      </View>

      <Section title={t('profile.account')}>
        <Surface>
          <ListRow icon="speedometer-outline" title={t('nav.usage')} onPress={() => router.push(USAGE_HREF)} />
          <ListRow icon="diamond-outline" title={t('nav.plans')} onPress={() => router.push(PLANS_HREF)} />
          <ListRow icon="card-outline" title={t('nav.billing')} onPress={() => router.push(BILLING_HREF)} />
          <ListRow icon="people-outline" title={t('nav.team')} onPress={() => router.push('/team')} />
          <ListRow icon="images-outline" title={t('creations.title')} onPress={() => router.push('/creations')} />
        </Surface>
      </Section>

      <Section title={t('profile.help')}>
        <Surface>
          <ListRow icon="help-buoy-outline" title={t('nav.support')} onPress={() => router.push(SUPPORT_HREF)} />
        </Surface>
      </Section>

      <Section title={t('profile.preferences')}>
        <Surface>
          {/* How TRINDATRON answers, what it remembers and where you are signed in — all from the Core. */}
          <ListRow icon="options-outline" title={t('nav.settings')} onPress={() => router.push(SETTINGS_HREF)} />
          <ListRow icon="shield-checkmark-outline" title={t('settings.privacyAndData')} onPress={() => router.push(SETTINGS_PRIVACY_HREF)} />
          {/* The app's own language, chosen on the device: it also localises screens the Core never sees. */}
          <ListRow icon="language-outline" title={t('profile.language')} subtitle={languageValue} onPress={() => setLanguageOpen(true)} />
        </Surface>
      </Section>

      <Section title={t('profile.app')}>
        <Surface>
          <ListRow icon="information-circle-outline" title={t('profile.version')} subtitle={Constants.expoConfig?.version ?? '—'} />
          {__DEV__ ? <ListRow icon="server-outline" title={t('profile.devServer')} subtitle={API_BASE_URL ?? '—'} /> : null}
        </Surface>
      </Section>

      <View style={styles.signOut}>
        <Button label={t('profile.signOut')} variant="danger" icon="log-out-outline" onPress={signOut} fullWidth />
        <AppText variant="caption" tone="muted" style={styles.center}>
          {t('profile.signOutNote')}
        </AppText>
      </View>

      <BottomSheet visible={languageOpen} onClose={() => setLanguageOpen(false)} title={t('profile.language')}>
        {LANGUAGE_OPTIONS.map((option) => (
          <SheetOption
            key={option}
            label={option === 'auto' ? t('profile.languageAuto') : t(`languages.${option}`)}
            detail={option === 'auto' ? t(`languages.${systemLocale}`) : null}
            selected={preference === option}
            onPress={() => {
              void chooseLanguage(option)
              setLanguageOpen(false)
            }}
          />
        ))}
      </BottomSheet>
    </Screen>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1, gap: 4 },
  center: { textAlign: 'center' },
  identity: { flexDirection: 'row', alignItems: 'center', gap: space.lg },
  avatar: { width: 64, height: 64, borderRadius: radius.full, alignItems: 'center', justifyContent: 'center' },
  signOut: { gap: space.sm },
})
