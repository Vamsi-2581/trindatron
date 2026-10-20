import { Stack, useLocalSearchParams } from 'expo-router'
import { useState } from 'react'
import { StyleSheet, View } from 'react-native'

import { AppText, Badge, Button, ErrorState, LoadingState, Notice, Screen, Surface, TextField } from '@/components/ui'
import { statusTone } from '@/features/support/status'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useT } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import { getTicket, replyToTicket, resolveTicket } from '@/services/api/support'
import { space, useTheme } from '@/theme'

/** One ticket as a conversation with the support team. */
export default function TicketScreen() {
  const t = useT()
  const { colors } = useTheme()
  const { ticketId } = useLocalSearchParams<{ ticketId: string }>()
  const ticket = useAsyncResource((signal) => getTicket(ticketId, signal), [ticketId], { refetchOnFocus: true })
  const [reply, setReply] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      ticket.refresh()
    } catch (cause) {
      setError(userMessage(cause))
    } finally {
      setBusy(false)
    }
  }

  if (ticket.loading && !ticket.data) {
    return (
      <Screen>
        <LoadingState />
      </Screen>
    )
  }
  if (!ticket.data) {
    return (
      <Screen>
        <ErrorState message={ticket.error?.message ?? t('error.generic')} onRetry={ticket.refresh} />
      </Screen>
    )
  }

  const data = ticket.data
  return (
    <Screen>
      <Stack.Screen options={{ title: data.subject }} />
      <View style={styles.header}>
        <AppText variant="title">{data.subject}</AppText>
        <View style={styles.row}>
          <Badge label={t(`support.status.${data.status}`)} tone={statusTone(data.status)} />
          <AppText variant="caption" tone="secondary">
            {t(`support.categories.${data.category}`)}
          </AppText>
        </View>
      </View>

      {(data.messages ?? []).map((message) => (
        <Surface
          key={message.id}
          style={[styles.message, message.author === 'support' ? { borderLeftColor: colors.accent, borderLeftWidth: 3 } : null]}
        >
          <AppText variant="caption" tone={message.author === 'support' ? 'accent' : 'secondary'}>
            {message.author === 'support' ? t('support.teamAuthor') : t('support.you')} · {new Date(message.created_at).toLocaleString()}
          </AppText>
          <AppText selectable>{message.body}</AppText>
        </Surface>
      ))}

      {error ? <Notice tone="error">{error}</Notice> : null}

      <Surface style={styles.message}>
        <TextField label={t('support.replyLabel')} value={reply} onChangeText={setReply} multiline maxLength={8000} />
        <View style={styles.actions}>
          {data.status !== 'resolved' ? (
            <Button label={t('support.resolve')} variant="ghost" disabled={busy} onPress={() => void run(() => resolveTicket(data.id))} />
          ) : null}
          <Button
            label={t('support.send')}
            icon="send-outline"
            loading={busy}
            disabled={!reply.trim()}
            onPress={() =>
              void run(async () => {
                await replyToTicket(data.id, reply.trim())
                setReply('')
              })
            }
          />
        </View>
      </Surface>
    </Screen>
  )
}

const styles = StyleSheet.create({
  header: { gap: space.sm },
  row: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  message: { padding: space.lg, gap: space.sm },
  actions: { flexDirection: 'row', justifyContent: 'flex-end', gap: space.sm },
})
