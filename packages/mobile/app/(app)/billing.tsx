import { router } from 'expo-router'
import { useState } from 'react'
import { View } from 'react-native'

import { AppText, Badge, Button, ErrorState, ListRow, Notice, Screen, Section, SkeletonRows, Surface } from '@/components/ui'
import { PLANS_HREF } from '@/features/navigation/routes'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useT } from '@/i18n'
import { CANCELLABLE_SUBSCRIPTION, cancelSubscription, listOrders, listSubscriptionsFromOrders } from '@/services/api/billing'
import { userMessage } from '@/services/api/errors'
import type { Subscription } from '@/services/api/types'
import { useAuthStore } from '@/stores/auth'
import { confirmAction } from '@/utils/confirm'
import { formatDateTime } from '@/utils/dates'
import { formatPrice, planLabel } from '@/utils/format'

/** A key the locale has, or the Core's own value when the app has no words for it yet. */
function labelOr(t: (key: string) => string, key: string, fallback: string): string {
  const label = t(key)
  return label === key ? fallback : label
}

/**
 * Billing: the current plan, the recurring subscription and how it is paid, and the payment history — all read
 * from the Core, and cancelled through the Core (POST /billing/subscriptions/{id}/cancel), never through support.
 * Choosing a plan is on the Plans screen.
 */
export default function BillingScreen() {
  const t = useT()
  const user = useAuthStore((state) => state.user)
  const orders = useAsyncResource((signal) => listOrders({ limit: 50 }, signal), [])
  const subscriptions = useAsyncResource(
    (signal) => (orders.data ? listSubscriptionsFromOrders(orders.data.orders, signal) : Promise.resolve([] as Subscription[])),
    [orders.data],
  )
  const [cancelling, setCancelling] = useState<string | null>(null)
  const [message, setMessage] = useState<{ tone: 'info' | 'error'; text: string } | null>(null)

  async function cancel(subscription: Subscription) {
    const confirmed = await confirmAction({
      title: t('billing.cancelTitle'),
      message: t('billing.cancelMessage', { plan: planLabel(subscription.plan) }),
      confirmLabel: t('billing.cancel'),
      destructive: true,
    })
    if (!confirmed) return
    setMessage(null)
    setCancelling(subscription.id)
    try {
      await cancelSubscription(subscription.id)
      setMessage({ tone: 'info', text: t('billing.cancelled') })
      void subscriptions.refresh()
    } catch (cause) {
      // The Core cancels at the provider first; an error means the subscription is still billing.
      setMessage({ tone: 'error', text: userMessage(cause) })
    } finally {
      setCancelling(null)
    }
  }

  const loadingOrders = orders.loading && !orders.data
  const history = orders.data?.orders.slice(0, 10) ?? []

  return (
    <Screen
      edges={['bottom', 'left', 'right']}
      refreshing={orders.refreshing || subscriptions.refreshing}
      onRefresh={() => {
        void orders.refresh()
      }}
    >
      <View>
        <AppText variant="label" tone="muted">
          {t('billing.currentPlan')}
        </AppText>
        <AppText variant="title">{planLabel(user?.plan)}</AppText>
      </View>
      <Button label={t('billing.seePlans')} icon="diamond-outline" variant="secondary" onPress={() => router.push(PLANS_HREF)} />

      {message ? <Notice tone={message.tone}>{message.text}</Notice> : null}

      <Section title={t('billing.subscription')}>
        {loadingOrders || (subscriptions.loading && !subscriptions.data) ? (
          <SkeletonRows count={2} />
        ) : orders.error && !orders.data ? (
          <ErrorState message={orders.error.message} onRetry={orders.refresh} />
        ) : subscriptions.error && !subscriptions.data ? (
          <ErrorState message={subscriptions.error.message} onRetry={subscriptions.refresh} />
        ) : (subscriptions.data?.length ?? 0) === 0 ? (
          <AppText variant="caption" tone="muted">
            {t('billing.noSubscription')}
          </AppText>
        ) : (
          <Surface>
            {subscriptions.data?.map((subscription) => {
              const cancellable = CANCELLABLE_SUBSCRIPTION.has(subscription.status)
              const details = [
                t('billing.perMonth', { price: formatPrice(subscription.amount, subscription.currency) }),
                labelOr(t, `billing.paymentMethod.${subscription.payment_method}`, subscription.payment_method),
                subscription.next_billing_at && cancellable
                  ? t('billing.nextBilling', { date: formatDateTime(subscription.next_billing_at) })
                  : subscription.current_period_end
                    ? t('billing.periodEnds', { date: formatDateTime(subscription.current_period_end) })
                    : null,
              ].filter(Boolean)
              return (
                <View key={subscription.id}>
                  <ListRow
                    icon="card-outline"
                    title={t('home.plan', { plan: planLabel(subscription.plan) })}
                    subtitle={details.join(' · ')}
                    trailing={
                      <Badge
                        label={labelOr(t, `billing.subscriptionStatus.${subscription.status}`, subscription.status)}
                        tone={
                          subscription.status === 'active'
                            ? 'success'
                            : subscription.status === 'cancelled' || subscription.status === 'expired'
                              ? 'neutral'
                              : 'warning'
                        }
                      />
                    }
                  />
                  {cancellable ? (
                    <Button
                      label={t('billing.cancel')}
                      variant="ghost"
                      loading={cancelling === subscription.id}
                      disabled={cancelling !== null && cancelling !== subscription.id}
                      onPress={() => cancel(subscription)}
                    />
                  ) : null}
                </View>
              )
            })}
          </Surface>
        )}
      </Section>

      <Section title={t('billing.history')}>
        {loadingOrders ? (
          <SkeletonRows count={4} />
        ) : history.length === 0 ? (
          <AppText variant="caption" tone="muted">
            {t('billing.noHistory')}
          </AppText>
        ) : (
          <Surface>
            {history.map((order) => (
              <ListRow
                key={order.id}
                icon="receipt-outline"
                title={order.plan ? t('home.plan', { plan: planLabel(order.plan) }) : t('billing.credits')}
                subtitle={`${formatPrice(order.amount_fiat, order.currency_fiat)} · ${formatDateTime(order.created_at)}`}
                trailing={
                  <Badge
                    label={t(`billing.orderStatus.${order.status}`)}
                    tone={order.status === 'confirmed' ? 'success' : order.status === 'pending' || order.status === 'processing' ? 'warning' : 'neutral'}
                  />
                }
              />
            ))}
          </Surface>
        )}
      </Section>
    </Screen>
  )
}
