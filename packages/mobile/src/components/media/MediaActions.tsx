import { router } from 'expo-router'
import { useState } from 'react'
import { StyleSheet, View } from 'react-native'

import { AppText, Button } from '@/components/ui'
import { useCapabilityStore } from '@/features/capabilities/store'
import { saveCreation, shareCreation, type CreationAction } from '@/features/media/creationFile'
import type { MediaCreation } from '@/features/media/media'
import { useT } from '@/i18n'
import { space } from '@/theme'

type Busy = 'save' | 'share' | null

/**
 * Open, download, share and create another — the same four actions right after a
 * creation, in the conversation, in Creations and after reopening a conversation,
 * because every one of them works from the Core job alone.
 */
export function MediaActions({
  media,
  showOpen = true,
  onCreateAnother,
}: {
  media: MediaCreation
  showOpen?: boolean
  onCreateAnother?: () => void
}) {
  const t = useT()
  const [busy, setBusy] = useState<Busy>(null)
  const [note, setNote] = useState<{ tone: 'secondary' | 'error'; text: string } | null>(null)
  const ready = media.status === 'completed' && Boolean(media.downloadPath)
  const kind = media.kind === 'video' ? 'video' : 'image'
  const canCreateAnother = useCapabilityStore((state) => state.capabilities[kind === 'video' ? 'video_generation' : 'image_generation'].available)

  async function run(which: Exclude<Busy, null>, action: (media: MediaCreation) => Promise<CreationAction>) {
    if (busy) return
    setBusy(which)
    setNote(null)
    try {
      const result = await action(media)
      if (result === 'saved') setNote({ tone: 'secondary', text: t('media.saved') })
    } catch {
      setNote({ tone: 'error', text: t('media.actionFailed') })
    } finally {
      setBusy(null)
    }
  }

  const createAnother = onCreateAnother ?? (() => router.push({ pathname: '/create', params: { kind } }))

  return (
    <View style={styles.wrap}>
      <View style={styles.row}>
        {ready && showOpen ? (
          <Button
            label={t('media.open')}
            variant="secondary"
            icon="expand-outline"
            onPress={() => router.push({ pathname: '/creation/[jobId]', params: { jobId: media.id } })}
          />
        ) : null}
        {ready ? (
          <Button label={t('media.download')} variant="secondary" icon="download-outline" loading={busy === 'save'} disabled={busy !== null} onPress={() => void run('save', saveCreation)} />
        ) : null}
        {ready ? (
          <Button label={t('media.share')} variant="secondary" icon="share-social-outline" loading={busy === 'share'} disabled={busy !== null} onPress={() => void run('share', shareCreation)} />
        ) : null}
        {/* Only when the Core can make another one here: an offer that leads to "indisponível" is a promise it
            cannot keep. Opening, saving and sharing what already exists stay. */}
        {(media.status === 'completed' || media.status === 'failed') && canCreateAnother ? (
          <Button label={t('media.another')} variant="secondary" icon="add" onPress={createAnother} />
        ) : null}
      </View>
      {note ? (
        <AppText variant="caption" tone={note.tone} accessibilityLiveRegion="polite">
          {note.text}
        </AppText>
      ) : null}
    </View>
  )
}

const styles = StyleSheet.create({
  wrap: { gap: space.xs },
  // One style for every action: a boxed "Abrir" beside link-like ghosts read as three kinds of control on one card.
  row: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm },
})
