import Ionicons from '@expo/vector-icons/Ionicons'
import * as Clipboard from 'expo-clipboard'
import { memo, useContext, useState } from 'react'
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native'

import { ChatMediaCard } from '@/components/media/ChatMediaCard'
import { AppText, Button, IconButton } from '@/components/ui'
import { useCapabilityStore } from '@/features/capabilities/store'
import type { ChatMessage } from '@/features/chat/chatReducer'
import { ContinueAnswerContext } from '@/features/chat/continueAnswer'
import { useSpeech } from '@/features/voice/useSpeech'
import { useTransientFlag } from '@/hooks/useTransientFlag'
import { useT } from '@/i18n'
import type { AgentStep } from '@/services/api/types'
import { font, iconSize, radius, space, useTheme } from '@/theme'

import { Markdown } from './Markdown'

interface MessageBubbleProps {
  message: ChatMessage
  activity: string | null
  agentSteps: AgentStep[]
  /** The newest message in the conversation: only there can a cut-off answer be continued. */
  latest?: boolean
}

export const MessageBubble = memo(function MessageBubble({ message, activity, agentSteps, latest = false }: MessageBubbleProps) {
  const { colors } = useTheme()
  const t = useT()
  const continueAnswer = useContext(ContinueAnswerContext)

  if (message.role === 'user') {
    return (
      <View style={styles.userRow}>
        <View style={[styles.userBubble, { backgroundColor: colors.userBubble }]}>
          <Text selectable style={[styles.userText, { color: colors.textPrimary }]}>
            {message.content}
          </Text>
        </View>
      </View>
    )
  }

  const streaming = message.state === 'streaming'
  return (
    <View style={styles.assistant}>
      {agentSteps.length > 0 ? <AgentSteps steps={agentSteps} /> : null}

      {message.media?.map((media) => <ChatMediaCard key={media.id} media={media} />)}

      {message.content ? (
        <Markdown content={message.content} />
      ) : streaming && !message.media?.length ? (
        <View style={styles.activity} accessibilityLiveRegion="polite">
          <ActivityIndicator size="small" color={colors.accent} />
          <AppText variant="caption" tone="secondary">
            {activity ?? t('chat.thinking')}
          </AppText>
        </View>
      ) : null}

      {streaming && message.content ? <ActivityIndicator size="small" color={colors.accent} style={styles.tail} /> : null}
      {message.state === 'stopped' ? (
        <AppText variant="caption" tone="muted">
          {t('chat.stopped')}
        </AppText>
      ) : null}
      {message.state === 'failed' ? (
        <AppText variant="caption" tone="error">
          {t('chat.incomplete')}
        </AppText>
      ) : null}
      {/* Said, not hidden: the answer ended at the plan's output limit, so it may stop mid-sentence or mid-table. */}
      {message.truncated && !streaming ? (
        <View style={styles.truncated}>
          <AppText variant="caption" tone="muted">
            {t('chat.truncated')}
          </AppText>
          {latest && continueAnswer ? (
            <Button label={t('chat.continue')} variant="secondary" icon="arrow-forward" onPress={continueAnswer} />
          ) : null}
        </View>
      ) : null}

      {!streaming && message.content ? <MessageActions content={message.content} /> : null}
    </View>
  )
})

function MessageActions({ content }: { content: string }) {
  const t = useT()
  const [copied, flashCopied] = useTransientFlag()
  const canListen = useCapabilityStore((state) => state.capabilities.audio_output.available)
  const speech = useSpeech()
  const speaking = speech.phase === 'playing'
  return (
    <View style={styles.actionsColumn}>
      <View style={styles.actions}>
        <IconButton
          icon={copied ? 'checkmark' : 'copy-outline'}
          label={copied ? t('chat.copied') : t('chat.copy')}
          size="md"
          onPress={async () => {
            await Clipboard.setStringAsync(content)
            flashCopied()
          }}
        />
        {/* Only when the Core says this account can hear an answer: a button that cannot speak is worse than none. */}
        {canListen ? (
          <IconButton
            icon={speech.phase === 'loading' ? 'ellipsis-horizontal' : speaking ? 'stop-circle-outline' : 'volume-high-outline'}
            label={speech.phase === 'loading' ? t('chat.preparingAudio') : speaking ? t('chat.listening') : t('chat.listen')}
            size="md"
            disabled={speech.phase === 'loading'}
            onPress={() => void speech.toggle(content)}
          />
        ) : null}
      </View>
      {speech.error ? (
        <AppText variant="caption" tone="error" accessibilityLiveRegion="polite">
          {speech.error}
        </AppText>
      ) : null}
      {speech.shortened && !speech.error ? (
        <AppText variant="caption" tone="muted" accessibilityLiveRegion="polite">
          {t('chat.readShortened')}
        </AppText>
      ) : null}
    </View>
  )
}

/** Shows that work happened, never which internal tool names or outputs were involved. */
function AgentSteps({ steps }: { steps: AgentStep[] }) {
  const { colors } = useTheme()
  const t = useT()
  const [open, setOpen] = useState(false)
  const calls = steps.filter((step) => step.type === 'tool_call')
  const summary = calls.length === 1 ? t('chat.actionsOne') : t('chat.actionsMany', { count: calls.length })
  return (
    <View style={[styles.steps, { backgroundColor: colors.surface1, borderColor: colors.separator }]}>
      <Pressable
        accessibilityRole="button"
        accessibilityState={{ expanded: open }}
        accessibilityLabel={summary}
        onPress={() => setOpen((value) => !value)}
        style={styles.stepsHeader}
      >
        <Ionicons name="construct-outline" size={iconSize.sm} color={colors.textSecondary} />
        <AppText variant="caption" tone="secondary" style={styles.flex}>
          {summary}
        </AppText>
        <Ionicons name={open ? 'chevron-up' : 'chevron-down'} size={iconSize.sm} color={colors.textMuted} />
      </Pressable>
      {open
        ? calls.map((step, index) => (
            <View key={index} style={styles.step}>
              <Ionicons name="checkmark-circle-outline" size={iconSize.sm} color={colors.textMuted} />
              <AppText variant="caption" tone="secondary" style={styles.flex}>
                {t(`activity.tool.${step.tool}`) === `activity.tool.${step.tool}` ? t('activity.tool.generic') : t(`activity.tool.${step.tool}`)}
              </AppText>
            </View>
          ))
        : null}
    </View>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  userRow: { alignItems: 'flex-end' },
  userBubble: {
    maxWidth: '88%',
    borderRadius: radius.xl,
    borderBottomRightRadius: radius.sm,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
  },
  userText: { fontSize: font.size.md, lineHeight: 24 },
  // Answers read full width, like a page; only the person's own words sit in a bubble.
  assistant: { gap: space.sm, width: '100%' },
  activity: { flexDirection: 'row', alignItems: 'center', gap: space.sm, minHeight: 28 },
  tail: { alignSelf: 'flex-start' },
  truncated: { gap: space.sm, alignItems: 'flex-start' },
  actions: { flexDirection: 'row', marginLeft: -space.sm },
  actionsColumn: { gap: space.xs },
  steps: { borderWidth: StyleSheet.hairlineWidth, borderRadius: radius.md, overflow: 'hidden' },
  stepsHeader: { flexDirection: 'row', alignItems: 'center', gap: space.sm, paddingHorizontal: space.md, minHeight: 36 },
  step: { flexDirection: 'row', gap: space.sm, paddingHorizontal: space.md, paddingBottom: space.sm },
})
