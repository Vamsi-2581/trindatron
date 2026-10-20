import { router, Stack, useLocalSearchParams } from 'expo-router'
import { useRef, useState } from 'react'
import { Platform, StyleSheet, TextInput, View } from 'react-native'

import { MediaResultCard } from '@/components/media/MediaResultCard'
import { AppText, Button, EmptyState, Notice, Screen } from '@/components/ui'
import { PLANS_HREF } from '@/features/navigation/routes'
import { statusOf } from '@/features/capabilities/capabilities'
import { useCapabilityStore } from '@/features/capabilities/store'
import { creationFromJob } from '@/features/media/media'
import { useMediaJob } from '@/features/media/useMediaJob'
import { useT } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import { createImage, createVideo, type MediaJobView } from '@/services/api/media'
import { font, radius, space, useTheme } from '@/theme'
import { createId } from '@/utils/ids'

/**
 * Creating an image or a video: describe it — format included, in any words
 * ("1080x1920", "9:16", "Instagram Story", "banner") — and create. The Core
 * reads the format, fits it to what the plan allows and runs the job in the
 * background; this screen follows it to the result. What the plan allows today
 * comes from the Core's capabilities, never the app.
 */
export default function CreateScreen() {
  const { colors } = useTheme()
  const t = useT()
  const params = useLocalSearchParams<{ kind?: string; prompt?: string }>()
  const kind: 'image' | 'video' = params.kind === 'video' ? 'video' : 'image'
  const capability = useCapabilityStore((state) => state.capabilities[kind === 'video' ? 'video_generation' : 'image_generation'])
  const [prompt, setPrompt] = useState(typeof params.prompt === 'string' ? params.prompt : '')
  const [format, setFormat] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [started, setStarted] = useState<MediaJobView | null>(null)
  const attemptKey = useRef(createId())
  const { job, error: followError } = useMediaJob(started)

  async function submit() {
    const text = prompt.trim()
    if (!text || busy) return
    setBusy(true)
    setError(null)
    try {
      // With no separate format, the description itself carries it ("vertical", "16:9"...).
      const input = { prompt: text, format: format.trim() || text }
      const accepted = kind === 'video' ? await createVideo(input, attemptKey.current) : await createImage(input, attemptKey.current)
      setStarted(accepted)
      // A new attempt gets a new key; a retried tap on the same attempt reuses this one.
      attemptKey.current = createId()
      void useCapabilityStore.getState().load(useCapabilityStore.getState().userId)
    } catch (cause) {
      setError(userMessage(cause))
    } finally {
      setBusy(false)
    }
  }

  const title = kind === 'video' ? t('composer.create.video') : t('composer.create.image')
  if (!capability.available && !started) {
    // The Core's matrix knows *why*, so this says why instead of guessing between reasons: a plan is
    // something the person can act on, an outage is something to wait out, and something unbuilt is
    // neither. Offering "see plans" during an outage would sell an upgrade that changes nothing.
    const status = statusOf(capability)
    const bodyKey = status === 'plan' ? 'create.planBody' : status === 'not_integrated' ? 'create.notYetBody' : 'create.unavailableBody'
    return (
      <Screen edges={['bottom', 'left', 'right']}>
        <Stack.Screen options={{ title }} />
        <EmptyState
          icon="sparkles-outline"
          title={t('create.unavailableTitle')}
          body={t(bodyKey)}
          action={
            status === 'plan' ? <Button label={t('tips.seePlans')} variant="secondary" onPress={() => router.push(PLANS_HREF)} /> : undefined
          }
        />
      </Screen>
    )
  }

  const quota = capability.dailyQuota
  const done = job?.status === 'completed' || job?.status === 'failed'
  const webOutline = Platform.OS === 'web' ? ({ outlineStyle: 'none' } as object) : null

  return (
    <Screen edges={['bottom', 'left', 'right']}>
      <Stack.Screen options={{ title }} />

      {job ? (
        <View style={styles.result}>
          {/* "Criar outra" lives on the card, the same action as everywhere else; here it starts a fresh request. */}
          <MediaResultCard media={creationFromJob(job)} onCreateAnother={() => setStarted(null)} />
          {followError ? <Notice tone="error">{followError}</Notice> : null}
          {!done ? (
            <AppText variant="caption" tone="muted" style={styles.center}>
              {kind === 'video' ? t('create.videoWait') : t('create.imageWait')}
            </AppText>
          ) : (
            <View style={styles.actions}>
              <Button label={t('creations.title')} variant="secondary" onPress={() => router.push('/creations')} />
            </View>
          )}
        </View>
      ) : (
        <>
          <View style={[styles.field, { backgroundColor: colors.surface1, borderColor: colors.border }]}>
            <TextInput
              value={prompt}
              onChangeText={setPrompt}
              placeholder={kind === 'video' ? t('create.videoPlaceholder') : t('create.imagePlaceholder')}
              placeholderTextColor={colors.textMuted}
              accessibilityLabel={t('create.promptA11y')}
              multiline
              maxLength={2000}
              autoFocus
              style={[styles.input, { color: colors.textPrimary }, webOutline]}
            />
          </View>

          <View style={styles.formatBlock}>
            <AppText variant="label" tone="muted">
              {t('create.format')}
            </AppText>
            <TextInput
              value={format}
              onChangeText={setFormat}
              placeholder={kind === 'video' ? t('create.videoFormatPlaceholder') : t('create.imageFormatPlaceholder')}
              placeholderTextColor={colors.textMuted}
              accessibilityLabel={t('create.format')}
              maxLength={120}
              autoCapitalize="none"
              autoCorrect={false}
              style={[styles.formatInput, { color: colors.textPrimary, backgroundColor: colors.surface1, borderColor: colors.border }, webOutline]}
            />
            <AppText variant="caption" tone="muted">
              {t('create.formatHint')}
            </AppText>
          </View>

          {quota ? (
            <AppText variant="caption" tone="secondary">
              {t(kind === 'video' ? 'usage.media.videosToday' : 'usage.media.imagesToday')} · {t('usage.media.remainingOf', { remaining: quota.remaining, limit: quota.limit })}
            </AppText>
          ) : null}
          {error ? <Notice tone="error">{error}</Notice> : null}
          <Button
            label={t('create.submit')}
            icon="sparkles"
            size="lg"
            fullWidth
            loading={busy}
            disabled={!prompt.trim() || (quota !== null && quota.remaining <= 0)}
            onPress={submit}
          />
        </>
      )}
    </Screen>
  )
}

const styles = StyleSheet.create({
  center: { textAlign: 'center' },
  field: { borderWidth: StyleSheet.hairlineWidth, borderRadius: radius.lg, padding: space.md },
  input: { fontSize: font.size.md, lineHeight: 24, minHeight: 120, maxHeight: 220, textAlignVertical: 'top' },
  formatBlock: { gap: space.xs },
  formatInput: {
    minHeight: 44,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: radius.md,
    paddingHorizontal: space.md,
    fontSize: font.size.md,
  },
  result: { gap: space.lg },
  actions: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm, justifyContent: 'center' },
})
