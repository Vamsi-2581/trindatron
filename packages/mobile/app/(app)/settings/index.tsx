import { router } from 'expo-router'
import { StyleSheet, View } from 'react-native'

import { AppText, ListRow, Notice, Screen, Section, SegmentedControl, Select, SkeletonRows, Surface } from '@/components/ui'
import { SETTINGS_DEVICES_HREF, SETTINGS_MEMORIES_HREF, SETTINGS_PRIVACY_HREF } from '@/features/navigation/routes'
import { chooseLanguage } from '@/features/settings/language'
import { usePreferences } from '@/features/settings/usePreferences'
import { useLocaleStore, useT } from '@/i18n'
import type { LanguagePreference } from '@/services/api/settings'
import { space } from '@/theme'

const LANGUAGES: LanguagePreference[] = ['auto', 'pt', 'en', 'es']

const STYLES = ['concise', 'balanced', 'detailed'] as const
type ResponseStyle = (typeof STYLES)[number]

/**
 * Settings: how TRINDATRON answers this person, and what it keeps about them.
 *
 * Every control here is wired to a Core contract — nothing is stored on the
 * device pretending to be a setting. The two the Core refuses (a colour theme
 * and a language for spoken input) are absent rather than shown as switches
 * that would do nothing.
 */
export default function SettingsScreen() {
  const t = useT()
  const { preferences, loading, error, saving, save } = usePreferences()
  const languagePreference = useLocaleStore((state) => state.preference)
  const systemLocale = useLocaleStore((state) => state.systemLocale)

  if (loading && !preferences) {
    return (
      <Screen edges={['bottom', 'left', 'right']}>
        <SkeletonRows count={5} />
      </Screen>
    )
  }

  return (
    <Screen edges={['bottom', 'left', 'right']}>
      {error ? <Notice tone="error">{error}</Notice> : null}

      <Section title={t('settings.answers')}>
        <Surface style={styles.card}>
          <View style={styles.field}>
            <AppText variant="bodyStrong">{t('profile.language')}</AppText>
            <Select
              accessibilityLabel={t('profile.language')}
              // Profile's labels, so the one setting reads the same in both places: "Automático" is the phone's
              // language (shown), never "the language I write in".
              options={LANGUAGES.map((language) => ({
                value: language,
                label: language === 'auto' ? `${t('profile.languageAuto')} · ${t(`languages.${systemLocale}`)}` : t(`languages.${language}`),
              }))}
              // The same choice as Profile's: the app's text and the answers (features/settings/language.ts).
              value={languagePreference}
              onChange={(language) => void chooseLanguage(language)}
            />
          </View>

          <View style={styles.field}>
            <AppText variant="bodyStrong">{t('settings.style')}</AppText>
            <SegmentedControl<ResponseStyle>
              accessibilityLabel={t('settings.style')}
              options={STYLES.map((style) => ({ value: style, label: t(`settings.styleValue.${style}`) }))}
              value={(preferences?.response_style as ResponseStyle | undefined) ?? null}
              disabled={!preferences || saving}
              onChange={(style) => void save({ response_style: style })}
            />
            <AppText variant="caption" tone="secondary">
              {t('settings.styleHint')}
            </AppText>
          </View>
        </Surface>
      </Section>

      <Section title={t('settings.privacy')}>
        <Surface>
          <ListRow icon="bookmark-outline" title={t('settings.memories')} onPress={() => router.push(SETTINGS_MEMORIES_HREF)} />
          <ListRow icon="shield-checkmark-outline" title={t('settings.privacyAndData')} onPress={() => router.push(SETTINGS_PRIVACY_HREF)} />
          <ListRow icon="phone-portrait-outline" title={t('settings.devices')} onPress={() => router.push(SETTINGS_DEVICES_HREF)} />
        </Surface>
      </Section>
    </Screen>
  )
}

const styles = StyleSheet.create({
  card: { padding: space.lg, gap: space.xl },
  field: { gap: space.sm },
})
