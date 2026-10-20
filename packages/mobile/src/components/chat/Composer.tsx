import Ionicons from '@expo/vector-icons/Ionicons'
import { router } from 'expo-router'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ActivityIndicator,
  Platform,
  Pressable,
  StyleSheet,
  TextInput,
  View,
  type NativeSyntheticEvent,
  type TextInputKeyPressEventData,
} from 'react-native'

import Animated, { useAnimatedStyle } from 'react-native-reanimated'
import { useReanimatedKeyboardAnimation } from 'react-native-keyboard-controller'

import { AppText, IconButton, PRESSED_SCALE } from '@/components/ui'
import { availableCreations, type MediaCapability } from '@/features/capabilities/capabilities'
import { useCapabilityStore } from '@/features/capabilities/store'
import { useConversationAttachments } from '@/features/files/useConversationAttachments'
import { useVoiceInput } from '@/features/voice/useVoiceInput'
import { useT } from '@/i18n'
import { font, iconSize, radius, space, TOUCH_TARGET, useTheme } from '@/theme'

import { AttachmentSheet } from './AttachmentSheet'

interface ComposerProps {
  /** Resolving `false` means the text was not accepted, so it is put back in the field. */
  onSend: (content: string) => void | Promise<boolean>
  /** A turn is in progress (or the conversation is opening). The composer waits; there is no "stop" action. */
  streaming: boolean
  disabled?: boolean
  /** Extra space under the composer (the home-indicator inset while the keyboard is closed). */
  bottomInset?: number
  placeholder?: string
  /** The conversation picked files are attached to; the Core reads documents into its context. */
  sessionId?: string | null
  /** Before the first message there is no conversation yet: this opens the one a picked file goes to. */
  ensureSession?: () => Promise<string>
  /** `docked` sits at the bottom of a screen; `inline` lives inside a page. */
  variant?: 'docked' | 'inline'
  /** Text to start the message with; a new `nonce` puts it back even when the text is the same. */
  draft?: { text: string; nonce: number } | null
}

function formatDuration(ms: number): string {
  const seconds = Math.floor(ms / 1000)
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

/**
 * One rounded container, the way AI products draw it: the files on their way (and attached) at the top, then
 * [ + ] [ Escreva… ] [ mic ] [ enviar ]. The "+" opens what this account can attach and create — the capability
 * matrix decides, never the model.
 */
export function Composer({
  onSend,
  streaming,
  disabled = false,
  bottomInset = 0,
  placeholder,
  sessionId = null,
  ensureSession,
  variant = 'docked',
  draft = null,
}: ComposerProps) {
  const { colors } = useTheme()
  const t = useT()
  const inputRef = useRef<TextInput>(null)
  const [text, setText] = useState('')
  const [sheetOpen, setSheetOpen] = useState(false)
  const capabilities = useCapabilityStore((state) => state.capabilities)
  // Only what the create screen really runs today; editing and image-to-video need a source picker there first.
  const creations = availableCreations(capabilities).filter((capability) => capability === 'image_generation' || capability === 'video_generation')

  const voice = useVoiceInput(useCallback((spoken: string) => setText((current) => (current ? `${current} ${spoken}` : spoken)), []))
  const files = useConversationAttachments(sessionId, ensureSession)
  const recording = voice.phase === 'recording'
  // A message never leaves while one of its files is still on the way: it would go without it.
  const canSend = text.trim().length > 0 && !streaming && !disabled && voice.phase === 'idle' && !files.uploading

  // A new draft (a suggestion picked) replaces the text once, applied while rendering rather than in an effect.
  const [appliedDraft, setAppliedDraft] = useState(draft)
  if (draft !== appliedDraft) {
    setAppliedDraft(draft)
    if (draft) setText(draft.text)
  }
  useEffect(() => {
    if (draft) inputRef.current?.focus()
  }, [draft])

  function submit() {
    if (!canSend) return
    const value = text
    setText('')
    const result = onSend(value)
    if (result) {
      void result.then((accepted) => {
        if (!accepted) setText((current) => current || value)
      })
    }
  }

  /** Creations run as Core media jobs on their own screen; the conversation cannot produce them. */
  function create(capability: MediaCapability) {
    const kind = capability === 'video_generation' ? 'video' : 'image'
    const prompt = text.trim()
    if (prompt) setText('')
    router.push({ pathname: '/create', params: prompt ? { kind, prompt } : { kind } })
  }

  // Web: Enter sends, Shift+Enter breaks the line — the Web app's convention.
  function onKeyPress(event: NativeSyntheticEvent<TextInputKeyPressEventData>) {
    const native = event.nativeEvent as TextInputKeyPressEventData & { shiftKey?: boolean }
    if (native.key === 'Enter' && !native.shiftKey) {
      event.preventDefault()
      submit()
    }
  }

  const docked = variant === 'docked'
  // The home-indicator/navigation-bar inset belongs under the composer only while the keyboard is not there, and the
  // keyboard arrives and leaves over ~400 ms. Following the keyboard's own progress (0 closed, 1 open) keeps the two in
  // step: a boolean that flips at keyboardDidHide left the composer sitting on the navigation bar for the whole
  // closing animation, and for as long as a screen mounted while the previous screen's keyboard was still up.
  const keyboard = useReanimatedKeyboardAnimation()
  const dockedStyle = useAnimatedStyle(() => ({ paddingBottom: space.sm + bottomInset * (1 - keyboard.progress.value) }))

  const failed = files.pending.find((item) => item.status === 'failed')
  const hasFiles = files.attached.length > 0 || files.pending.length > 0
  // Idle and not ready reads as a quiet control on either theme; accent is kept for when there is something to send.
  const sendIdle = !canSend && !streaming

  return (
    <Animated.View style={[styles.container, docked ? dockedStyle : styles.inline]}>
      {voice.error ? (
        <AppText variant="caption" tone="error" accessibilityLiveRegion="polite" style={styles.message}>
          {voice.error}
        </AppText>
      ) : null}
      {failed ? (
        <AppText variant="caption" tone="error" accessibilityLiveRegion="polite" style={styles.message}>
          {failed.error}
        </AppText>
      ) : null}
      {files.error ? (
        <AppText variant="caption" tone="error" accessibilityLiveRegion="polite" style={styles.message}>
          {files.error}
        </AppText>
      ) : null}

      <View style={[styles.box, { backgroundColor: colors.inputBackground, borderColor: colors.border }]}>
        {hasFiles ? (
          <View style={styles.chips}>
            {files.attached.map((file) => (
              <View key={file.id} style={[styles.chip, { backgroundColor: colors.accentMuted }]}>
                <Ionicons name={file.kind === 'image' ? 'image-outline' : 'document-attach-outline'} size={iconSize.sm} color={colors.accentText} />
                <AppText variant="caption" tone="accent" numberOfLines={1} style={styles.chipText}>
                  {file.readable ? file.name : t('composer.notRead', { name: file.name })}
                </AppText>
                <IconButton
                  icon="close"
                  size="sm"
                  label={t('composer.removeAttachmentA11y', { name: file.name })}
                  onPress={() => void files.remove(file.id)}
                  style={styles.chipClose}
                />
              </View>
            ))}
            {files.pending.map((item) => {
              const uploading = item.status === 'uploading'
              return (
                <View
                  key={item.id}
                  style={[styles.chip, { backgroundColor: colors.surface2, borderColor: uploading ? 'transparent' : colors.error, borderWidth: StyleSheet.hairlineWidth }]}
                >
                  {uploading ? <ActivityIndicator size="small" color={colors.accent} /> : <Ionicons name="alert-circle-outline" size={iconSize.sm} color={colors.error} />}
                  <AppText variant="caption" tone={uploading ? 'primary' : 'error'} numberOfLines={1} style={styles.chipText}>
                    {uploading ? t('upload.progress', { percent: Math.round(item.progress * 100) }) : item.file.name}
                  </AppText>
                  {uploading ? null : (
                    <IconButton icon="refresh" size="sm" label={t('upload.retryA11y', { name: item.file.name })} onPress={() => files.retry(item)} style={styles.chipClose} />
                  )}
                  <IconButton
                    icon="close"
                    size="sm"
                    label={uploading ? t('upload.cancelA11y', { name: item.file.name }) : t('upload.dismissA11y', { name: item.file.name })}
                    onPress={() => (uploading ? files.cancel(item.id) : files.dismiss(item.id))}
                    style={styles.chipClose}
                  />
                </View>
              )
            })}
          </View>
        ) : null}

        {recording ? (
          <View style={styles.row} accessibilityLiveRegion="polite">
            <IconButton icon="trash-outline" label={t('chat.recordCancel')} tone="danger" onPress={voice.cancel} />
            <View style={styles.recording}>
              <View style={[styles.dot, { backgroundColor: colors.error }]} />
              <AppText variant="bodyStrong">{formatDuration(voice.durationMs)}</AppText>
              <AppText variant="caption" tone="secondary" numberOfLines={1} style={styles.flex}>
                {t('chat.recordingHint')}
              </AppText>
            </View>
            <Pressable
              accessibilityRole="button"
              accessibilityLabel={t('chat.recordStop')}
              onPress={voice.stop}
              style={({ pressed }) => [styles.send, { backgroundColor: pressed ? colors.accentPressed : colors.accent, transform: [{ scale: pressed ? PRESSED_SCALE : 1 }] }]}
            >
              <Ionicons name="checkmark" size={iconSize.md} color={colors.onAccent} />
            </Pressable>
          </View>
        ) : (
          <View style={styles.row}>
            <IconButton icon="add" label={t('composer.add')} disabled={disabled} onPress={() => setSheetOpen(true)} />
            <TextInput
              ref={inputRef}
              value={text}
              onChangeText={setText}
              placeholder={placeholder ?? t('chat.placeholder')}
              placeholderTextColor={colors.textMuted}
              selectionColor={colors.accent}
              multiline
              scrollEnabled
              editable={!disabled}
              accessibilityLabel={t('chat.inputA11y')}
              onKeyPress={Platform.OS === 'web' ? onKeyPress : undefined}
              style={[styles.input, { color: colors.textPrimary }, Platform.OS === 'web' ? ({ outlineStyle: 'none' } as object) : null]}
            />

            {voice.phase === 'transcribing' ? (
              <View style={styles.side} accessibilityLabel={t('chat.transcribing')}>
                <ActivityIndicator color={colors.accent} />
              </View>
            ) : (
              <IconButton icon="mic-outline" label={t('chat.recordStart')} size="md" disabled={streaming || disabled} onPress={voice.start} />
            )}

            <Pressable
              accessibilityRole="button"
              accessibilityLabel={streaming ? t('chat.replying') : t('chat.send')}
              accessibilityState={{ disabled: !canSend, busy: streaming }}
              disabled={!canSend}
              onPress={submit}
              style={({ pressed }) => [
                styles.send,
                {
                  backgroundColor: sendIdle ? colors.surface3 : pressed ? colors.accentPressed : colors.accent,
                  transform: [{ scale: pressed ? PRESSED_SCALE : 1 }],
                },
              ]}
            >
              {streaming ? (
                <ActivityIndicator size="small" color={colors.onAccent} />
              ) : (
                <Ionicons name="arrow-up" size={iconSize.md} color={sendIdle ? colors.textMuted : colors.onAccent} />
              )}
            </Pressable>
          </View>
        )}
      </View>

      <AttachmentSheet
        visible={sheetOpen}
        onClose={() => setSheetOpen(false)}
        creations={creations}
        onCreate={create}
        onAttach={(file) => void files.attach(file)}
      />
    </Animated.View>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  container: { paddingHorizontal: space.md, paddingTop: space.sm, gap: space.xs },
  inline: { paddingHorizontal: 0, paddingTop: 0 },
  message: { paddingHorizontal: space.xs, width: '100%', maxWidth: 760, alignSelf: 'center' },
  box: {
    width: '100%',
    maxWidth: 760,
    alignSelf: 'center',
    borderRadius: 24,
    borderWidth: StyleSheet.hairlineWidth,
    padding: space.xs,
    gap: space.xs,
  },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: space.xs, paddingHorizontal: space.xs, paddingTop: space.xs },
  chip: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.xs,
    maxWidth: '100%',
    minHeight: 32,
    paddingLeft: space.sm,
    borderRadius: radius.full,
  },
  chipText: { flexShrink: 1 },
  chipClose: { width: 32, height: 32 },
  recording: { flex: 1, flexDirection: 'row', alignItems: 'center', gap: space.sm, minHeight: TOUCH_TARGET },
  dot: { width: 8, height: 8, borderRadius: radius.full },
  row: { flexDirection: 'row', alignItems: 'flex-end' },
  input: { flex: 1, fontSize: font.size.md, lineHeight: 22, minHeight: TOUCH_TARGET, maxHeight: 140, paddingTop: 11, paddingBottom: 11, paddingHorizontal: 2 },
  side: { width: TOUCH_TARGET, height: TOUCH_TARGET, alignItems: 'center', justifyContent: 'center' },
  send: { width: 40, height: 40, borderRadius: radius.full, alignItems: 'center', justifyContent: 'center', margin: 2 },
})
