import { router } from 'expo-router'
import { useState } from 'react'
import { StyleSheet } from 'react-native'

import { AppText, Button, ListRow, Notice, Screen, Section, Surface } from '@/components/ui'
import { SETTINGS_DEVICES_HREF, SETTINGS_MEMORIES_HREF } from '@/features/navigation/routes'
import { shareMyData } from '@/features/settings/exportFile'
import { useT } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import { space } from '@/theme'

/**
 * What is kept, why, and how to take it or remove it.
 *
 * The three things the Core really separates are named as three different
 * things: the messages of one conversation, the memories that carry across
 * conversations, and the aggregate counters that carry no content at all.
 * Account deletion is deliberately absent — the Core has no such endpoint, and
 * a button that opens a support ticket pretending otherwise would be a lie.
 */
export default function PrivacyScreen() {
  const t = useT()
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState<{ tone: 'info' | 'error'; text: string } | null>(null)

  async function exportData() {
    setBusy(true)
    setNote(null)
    try {
      await shareMyData()
    } catch (cause) {
      setNote({ tone: 'error', text: userMessage(cause) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Screen edges={['bottom', 'left', 'right']}>
      {note ? <Notice tone={note.tone}>{note.text}</Notice> : null}

      <Section title={t('privacy.whatIsKept')}>
        <Surface style={styles.card}>
          <AppText variant="bodyStrong">{t('privacy.conversationTitle')}</AppText>
          <AppText variant="caption" tone="secondary">
            {t('privacy.conversationBody')}
          </AppText>
          <AppText variant="bodyStrong">{t('privacy.memoryTitle')}</AppText>
          <AppText variant="caption" tone="secondary">
            {t('privacy.memoryBody')}
          </AppText>
          <AppText variant="bodyStrong">{t('privacy.productTitle')}</AppText>
          <AppText variant="caption" tone="secondary">
            {t('privacy.productBody')}
          </AppText>
        </Surface>
      </Section>

      <Section title={t('privacy.yourData')}>
        <Surface style={styles.card}>
          <AppText variant="caption" tone="secondary">
            {t('privacy.exportBody')}
          </AppText>
          <Button
            label={t('privacy.export')}
            icon="download-outline"
            variant="secondary"
            loading={busy}
            onPress={() => void exportData()}
          />
        </Surface>
        <Surface>
          <ListRow icon="bookmark-outline" title={t('settings.memories')} onPress={() => router.push(SETTINGS_MEMORIES_HREF)} />
          <ListRow icon="phone-portrait-outline" title={t('settings.devices')} onPress={() => router.push(SETTINGS_DEVICES_HREF)} />
        </Surface>
      </Section>

      <AppText variant="caption" tone="muted">
        {t('privacy.deletionNote')}
      </AppText>
    </Screen>
  )
}

const styles = StyleSheet.create({
  card: { padding: space.lg, gap: space.xs },
})
