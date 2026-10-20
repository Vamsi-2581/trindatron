import * as WebBrowser from 'expo-web-browser'
import { useRef, useState } from 'react'
import { Linking, Platform, View } from 'react-native'

import { AppText, Badge, Button, ErrorState, ListRow, Notice, Screen, Section, SkeletonRows, Surface } from '@/components/ui'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useT } from '@/i18n'
import { createCheckout, getOrder, listPaymentProviders, listSellablePlans } from '@/services/api/billing'
import { userMessage } from '@/services/api/errors'
import type { PaymentOrder, PaymentOrderStatus } from '@/services/api/types'
import { useAuthStore } from '@/stores/auth'
import { formatPrice, planLabel } from '@/utils/format'
import { createId } from '@/utils/ids'

const SUPPORT_EMAIL = 'contato@trinity.ai'
const POLL_ATTEMPTS = 10
const POLL_MS = 3000
const FINAL: ReadonlySet<PaymentOrderStatus> = new Set(['confirmed', 'failed', 'cancelled', 'expired', 'refunded'])

/**
 * iOS: digital plans sold inside an iOS app must go through Apple's In-App
 * Purchase, which TRINITY CORE does not integrate. So the iOS build shows the
 * plans but offers no purchase path (App Store Review 3.1.1).
 */
const PURCHASE_AVAILABLE = Platform.OS !== 'ios'

async function waitForOrder(orderId: string): Promise<PaymentOrder | null> {
  let last: PaymentOrder | null = null
  for (let attempt = 0; attempt < POLL_ATTEMPTS; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, POLL_MS))
    try {
      last = await getOrder(orderId)
      if (FINAL.has(last.status)) return last
    } catch {
      // Transient — keep polling; the Core confirms through the provider's webhook.
    }
  }
  return last
}

/**
 * The plans and their prices, exactly as the Core's catalog returns them (GET /billing/plans): name, price and
 * currency. The catalog is monthly (src/billing/pricing.py) and carries no feature list, so none is shown here —
 * what a plan includes is the Core's to say, not the app's to invent.
 */
export default function PlansScreen() {
  const t = useT()
  const user = useAuthStore((state) => state.user)
  const plans = useAsyncResource((signal) => listSellablePlans(signal), [])
  const providers = useAsyncResource((signal) => listPaymentProviders(signal), [])
  const idempotencyKeys = useRef<Record<string, string>>({})
  const [busyPlan, setBusyPlan] = useState<string | null>(null)
  const [message, setMessage] = useState<{ tone: 'info' | 'error'; text: string } | null>(null)

  const onlinePayment = (providers.data?.providers.length ?? 0) > 0

  async function choose(plan: string) {
    setMessage(null)
    const label = planLabel(plan)
    if (!onlinePayment) {
      const subject = encodeURIComponent(t('billing.upgradeSubject', { plan: label }))
      const body = encodeURIComponent(t('billing.upgradeBody', { plan: label, email: user?.email ?? '' }))
      Linking.openURL(`mailto:${SUPPORT_EMAIL}?subject=${subject}&body=${body}`).catch(() => undefined)
      return
    }

    // One key per plan attempt, reused on retry, so a double tap never creates two orders.
    const key = (idempotencyKeys.current[plan] ??= createId())
    setBusyPlan(plan)
    try {
      const checkout = await createCheckout(plan, key)
      if (checkout.redirect_url) await WebBrowser.openBrowserAsync(checkout.redirect_url)
      const order = checkout.order.status === 'confirmed' ? checkout.order : await waitForOrder(checkout.order.id)
      if (order?.status === 'confirmed') {
        delete idempotencyKeys.current[plan]
        await useAuthStore.getState().refreshUser()
        setMessage({ tone: 'info', text: t('billing.activated', { plan: label }) })
      } else if (order && FINAL.has(order.status)) {
        delete idempotencyKeys.current[plan]
        setMessage({ tone: 'error', text: t('billing.notCompleted') })
      } else {
        setMessage({ tone: 'info', text: t('billing.processing') })
      }
    } catch (cause) {
      setMessage({ tone: 'error', text: userMessage(cause) })
    } finally {
      setBusyPlan(null)
    }
  }

  return (
    <Screen
      edges={['bottom', 'left', 'right']}
      refreshing={plans.refreshing}
      onRefresh={() => {
        void plans.refresh()
        void providers.refresh()
      }}
    >
      <View>
        <AppText variant="label" tone="muted">
          {t('billing.currentPlan')}
        </AppText>
        <AppText variant="title">{planLabel(user?.plan)}</AppText>
      </View>

      {message ? <Notice tone={message.tone}>{message.text}</Notice> : null}

      {!PURCHASE_AVAILABLE ? (
        <Notice tone="info">{t('billing.iosUnavailable')}</Notice>
      ) : !providers.loading && !onlinePayment ? (
        <Notice tone="info">{t('billing.onlineUnavailable')}</Notice>
      ) : null}

      <Section title={t('billing.plans')}>
        {plans.loading && !plans.data ? (
          <SkeletonRows count={3} />
        ) : plans.error && !plans.data ? (
          <ErrorState message={plans.error.message} onRetry={plans.refresh} />
        ) : (
          <Surface>
            {plans.data?.map((price) => {
              const current = user?.plan === price.plan
              return (
                <ListRow
                  key={price.plan}
                  icon="diamond-outline"
                  title={planLabel(price.plan)}
                  subtitle={t('billing.perMonth', { price: formatPrice(price.amount, price.currency) })}
                  trailing={
                    current ? (
                      <Badge label={t('billing.current')} tone="accent" />
                    ) : PURCHASE_AVAILABLE ? (
                      <Button
                        label={onlinePayment ? t('billing.subscribe') : t('billing.request')}
                        variant="ghost"
                        loading={busyPlan === price.plan}
                        disabled={busyPlan !== null && busyPlan !== price.plan}
                        onPress={() => choose(price.plan)}
                      />
                    ) : undefined
                  }
                />
              )
            })}
          </Surface>
        )}
      </Section>
    </Screen>
  )
}
