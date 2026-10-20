import { router } from 'expo-router'
import { useState } from 'react'
import { StyleSheet, View } from 'react-native'

import { AppText, BrandSymbol, Button, EmptyState, Notice, Screen, SkeletonRows, Surface } from '@/components/ui'
import { PLANS_HREF } from '@/features/navigation/routes'
import { combinedStatus } from '@/features/capabilities/combinedStatus'
import { useCapabilityStore } from '@/features/capabilities/store'
import { useT } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import { createSession } from '@/services/api/sessions'
import { usePendingMessageStore } from '@/stores/pendingMessage'
import { space } from '@/theme'

const EXAMPLE_KEYS = ['findOption', 'signUp', 'comparePrices', 'fillForm'] as const

/**
 * Computer Use: say what you want done, and TRINDATRON does it.
 *
 * The phone starts the request and watches it happen — the work itself runs in
 * the Core, in an isolated browser or virtual computer, never on this device.
 * There is no local executor here and there must never be one: the honest
 * version of this screen is a way to ask, plus the same approval gate the
 * conversation already enforces before anything is clicked or changed.
 */
export default function ComputerUseScreen() {
  const t = useT()
  const browser = useCapabilityStore((state) => state.capabilities.browser_control)
  const computer = useCapabilityStore((state) => state.capabilities.computer_control)
  const loaded = useCapabilityStore((state) => state.loaded)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const status = combinedStatus(browser, computer)
  const usable = status === 'available'

  async function start(request: string) {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const session = await createSession({ title: t('computerUse.conversationTitle') })
      usePendingMessageStore.getState().put(session.id, request)
      router.push({ pathname: '/chat/[sessionId]', params: { sessionId: session.id } })
    } catch (cause) {
      setError(userMessage(cause))
    } finally {
      setBusy(false)
    }
  }

  // Before the Core has answered, every capability still reads as off; saying "unavailable" then was a guess.
  if (!loaded) {
    return (
      <Screen edges={['bottom', 'left', 'right']}>
        <SkeletonRows count={3} />
      </Screen>
    )
  }

  if (!usable) {
    return (
      <Screen edges={['bottom', 'left', 'right']}>
        {/* Three different situations, each said as it is: not in the plan (and where to change that), in the plan
            but not running where this app is connected, or not part of the product. One "Ainda não disponível aqui"
            for all three hid which one it was, and promised a retry that could not help. */}
        <EmptyState
          icon="desktop-outline"
          title={t(`computerUse.state.${status}.title`)}
          body={t(`computerUse.state.${status}.body`)}
          action={status === 'plan' ? <Button label={t('usage.seePlans')} onPress={() => router.push(PLANS_HREF)} /> : undefined}
        />
      </Screen>
    )
  }

  return (
    <Screen edges={['bottom', 'left', 'right']}>
      {error ? <Notice tone="error">{error}</Notice> : null}

      <View style={styles.hero}>
        <BrandSymbol size={56} />
        <AppText variant="heading" style={styles.center}>
          {t('computerUse.headline')}
        </AppText>
        <AppText tone="secondary" style={styles.center}>
          {t('computerUse.subhead')}
        </AppText>
      </View>

      <Surface style={styles.card}>
        <AppText variant="caption" tone="secondary">
          {t('computerUse.howItWorks')}
        </AppText>
        <AppText variant="caption" tone="muted">
          {t('computerUse.isolationNote')}
        </AppText>
      </Surface>

      <AppText variant="label" tone="muted">
        {t('computerUse.tryThis')}
      </AppText>
      <View style={styles.examples}>
        {EXAMPLE_KEYS.map((key) => (
          <Button
            key={key}
            label={t(`computerUse.example.${key}`)}
            variant="secondary"
            disabled={busy}
            onPress={() => void start(t(`computerUse.example.${key}`))}
          />
        ))}
      </View>

      <AppText variant="caption" tone="muted" style={styles.center}>
        {t('computerUse.approvalNote')}
      </AppText>
    </Screen>
  )
}

const styles = StyleSheet.create({
  center: { textAlign: 'center' },
  hero: { alignItems: 'center', gap: space.sm, paddingVertical: space.lg },
  card: { padding: space.lg, gap: space.xs },
  examples: { gap: space.sm },
})
