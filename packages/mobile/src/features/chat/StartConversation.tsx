import { router } from 'expo-router'
import { useCallback, useRef, useState } from 'react'
import { StyleSheet, View } from 'react-native'
import { KeyboardController } from 'react-native-keyboard-controller'

import { Composer } from '@/components/chat/Composer'
import { AppText } from '@/components/ui'
import { useT } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import { createSession } from '@/services/api/sessions'
import { usePendingMessageStore } from '@/stores/pendingMessage'
import { space } from '@/theme'

/**
 * The composer before a conversation exists (Home, a project, an agent). The
 * conversation opens on the first thing that needs it — a picked file or the
 * message — and the chat screen then opens already sending, with the files
 * attached to that same conversation.
 */
export function StartConversation({
  projectId = null,
  agentDefinitionId = null,
  placeholder,
  variant = 'inline',
  bottomInset = 0,
  draft = null,
}: {
  projectId?: string | null
  agentDefinitionId?: string | null
  placeholder?: string
  variant?: 'docked' | 'inline'
  bottomInset?: number
  /** Text to start the message with (a suggestion); a new `nonce` puts it back even when the text is the same. */
  draft?: { text: string; nonce: number } | null
}) {
  const t = useT()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sessionId, setSessionId] = useState<string | null>(null)
  // A fresh composer, with no files left over, after each conversation it opens.
  const [generation, setGeneration] = useState(0)
  const opening = useRef<Promise<string> | null>(null)

  const ensureSession = useCallback(() => {
    if (!opening.current) {
      const request = createSession({ projectId, agentDefinitionId }).then((session) => {
        setSessionId(session.id)
        return session.id
      })
      // A failed opening can be tried again by the next file or message.
      request.catch(() => {
        if (opening.current === request) opening.current = null
      })
      opening.current = request
    }
    return opening.current
  }, [projectId, agentDefinitionId])

  async function start(content: string): Promise<boolean> {
    const text = content.trim()
    if (!text || busy) return false
    setBusy(true)
    setError(null)
    // Closed here, while this screen is on top and hears it close: carried into the conversation, the keyboard was
    // closed there instead, and this screen, left behind, kept its room for it — back on Home the composer and the
    // greeting stayed raised over an empty space (Android 16, physical device). It also closes while the
    // conversation is being created, instead of sitting over a screen that is about to change.
    void KeyboardController.dismiss()
    try {
      const id = await ensureSession()
      usePendingMessageStore.getState().put(id, text)
      router.push({ pathname: '/chat/[sessionId]', params: { sessionId: id } })
      opening.current = null
      setSessionId(null)
      setGeneration((value) => value + 1)
      return true
    } catch (cause) {
      setError(userMessage(cause))
      return false
    } finally {
      setBusy(false)
    }
  }

  return (
    <View style={styles.root}>
      {error ? (
        <AppText variant="caption" tone="error" accessibilityLiveRegion="polite" style={styles.error}>
          {error}
        </AppText>
      ) : null}
      <Composer
        key={generation}
        variant={variant}
        bottomInset={bottomInset}
        onSend={start}
        streaming={busy}
        placeholder={placeholder ?? t('chat.placeholder')}
        draft={draft}
        sessionId={sessionId}
        ensureSession={ensureSession}
      />
    </View>
  )
}

const styles = StyleSheet.create({
  root: { gap: space.xs },
  error: { paddingHorizontal: space.lg },
})
