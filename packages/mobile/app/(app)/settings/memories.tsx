import { useState } from 'react'
import { StyleSheet, View } from 'react-native'

import { AppText, Button, EmptyState, ErrorState, IconButton, SkeletonRows, Notice, Screen, Surface, Toggle } from '@/components/ui'
import { usePreferences } from '@/features/settings/usePreferences'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useT } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import { clearMemories, deleteMemory, listMemories } from '@/services/api/settings'
import { space } from '@/theme'
import { confirmAction } from '@/utils/confirm'
import { formatDateTime } from '@/utils/dates'

/**
 * What TRINDATRON remembers about this person, and the two controls that matter:
 * stop using it, or delete it. Switching memory off keeps what was saved —
 * deleting is a separate, explicit act, so neither is a surprise.
 */
export default function MemoriesScreen() {
  const t = useT()
  const { preferences, saving, save } = usePreferences()
  const memories = useAsyncResource((signal) => listMemories(signal), [], { refetchOnFocus: true })
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function removeOne(id: string) {
    setBusy(id)
    setError(null)
    try {
      await deleteMemory(id)
      memories.setData((current) => (current ?? []).filter((memory) => memory.id !== id))
    } catch (cause) {
      setError(userMessage(cause))
    } finally {
      setBusy(null)
    }
  }

  async function removeAll() {
    const confirmed = await confirmAction({
      title: t('memories.clearTitle'),
      message: t('memories.clearMessage'),
      confirmLabel: t('memories.clear'),
      destructive: true,
    })
    if (!confirmed) return
    setBusy('all')
    setError(null)
    try {
      await clearMemories()
      memories.setData([])
    } catch (cause) {
      setError(userMessage(cause))
    } finally {
      setBusy(null)
    }
  }

  const list = memories.data ?? []

  return (
    <Screen edges={['bottom', 'left', 'right']} refreshing={memories.refreshing} onRefresh={memories.refresh}>
      {error ? <Notice tone="error">{error}</Notice> : null}

      <Surface style={styles.card}>
        <Toggle
          label={t('memories.useMemory')}
          // Describes the state the switch is in: one fixed sentence read "Off" next to a switch that was on.
          description={t(preferences?.memory_enabled ? 'memories.useMemoryOn' : 'memories.useMemoryOff')}
          value={Boolean(preferences?.memory_enabled)}
          disabled={!preferences || saving}
          onValueChange={(next) => void save({ memory_enabled: next })}
        />
      </Surface>

      {memories.loading && !memories.data ? (
        <SkeletonRows count={4} />
      ) : memories.error && !memories.data ? (
        <ErrorState message={memories.error.message} onRetry={memories.refresh} />
      ) : list.length === 0 ? (
        <EmptyState icon="bookmark-outline" title={t('memories.emptyTitle')} body={t('memories.emptyBody')} />
      ) : (
        <View style={styles.list}>
          {list.map((memory) => (
            <Surface key={memory.id} style={styles.memory}>
              <View style={styles.memoryRow}>
                <AppText style={styles.flex}>{memory.content}</AppText>
                <IconButton
                  icon="trash-outline"
                  tone="danger"
                  label={t('memories.deleteA11y')}
                  disabled={busy !== null}
                  onPress={() => void removeOne(memory.id)}
                />
              </View>
              <AppText variant="caption" tone="muted">
                {formatDateTime(memory.created_at)}
              </AppText>
            </Surface>
          ))}
          <Button
            label={t('memories.clear')}
            variant="danger"
            icon="trash-outline"
            loading={busy === 'all'}
            disabled={busy !== null}
            onPress={() => void removeAll()}
          />
        </View>
      )}
    </Screen>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  card: { padding: space.lg },
  list: { gap: space.md },
  memory: { padding: space.lg, gap: space.xs },
  memoryRow: { flexDirection: 'row', alignItems: 'flex-start', gap: space.sm },
})
