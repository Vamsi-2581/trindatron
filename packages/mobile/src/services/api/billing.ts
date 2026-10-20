import { apiRequest } from './client'
import type { CheckoutResponse, PaymentOrder, PlanPrice, Subscription } from './types'

/**
 * Billing is read and initiated here, never processed. Prices come from the
 * Core's catalog; payment happens on the provider's hosted page; the plan only
 * changes when the Core confirms the order.
 */

export function listPlanPrices(signal?: AbortSignal): Promise<PlanPrice[]> {
  return apiRequest<PlanPrice[]>('/billing/plans', { signal })
}

/**
 * Whether a catalog entry is a commercial offer the app may show.
 *
 * TEAM is sold per seat (Standard and Premium, docs/TEAM_SEATS.md in the Core),
 * but the Core's catalog still returns the retired flat TEAM entry. Until the
 * catalog carries seat pricing, that entry is not an offer and is never shown —
 * this is the only place that decides it; update it when the Core ships seats.
 */
export function isCommerciallyAvailable(price: PlanPrice): boolean {
  return price.plan !== 'team'
}

export async function listSellablePlans(signal?: AbortSignal): Promise<PlanPrice[]> {
  return (await listPlanPrices(signal)).filter(isCommerciallyAvailable)
}

export function listPaymentProviders(signal?: AbortSignal): Promise<{ providers: string[] }> {
  return apiRequest('/billing/providers', { signal })
}

export function listOrders(options: { limit?: number } = {}, signal?: AbortSignal): Promise<{ orders: PaymentOrder[]; total: number }> {
  const query = options.limit ? `?limit=${options.limit}` : ''
  return apiRequest(`/billing/orders${query}`, { signal })
}

export function getOrder(orderId: string, signal?: AbortSignal): Promise<PaymentOrder> {
  return apiRequest<PaymentOrder>(`/billing/orders/${encodeURIComponent(orderId)}`, { signal })
}

export function getSubscription(subscriptionId: string, signal?: AbortSignal): Promise<Subscription> {
  return apiRequest<Subscription>(`/billing/subscriptions/${encodeURIComponent(subscriptionId)}`, { signal })
}

/** The Core asks the provider to stop billing first; a failure means nothing was cancelled. */
export function cancelSubscription(subscriptionId: string): Promise<Subscription> {
  return apiRequest<Subscription>(`/billing/subscriptions/${encodeURIComponent(subscriptionId)}/cancel`, { method: 'POST' })
}

/** States the Core can still cancel (SubscriptionService's transition table). */
export const CANCELLABLE_SUBSCRIPTION: ReadonlySet<string> = new Set(['active', 'past_due', 'grace_period'])

/**
 * The person's subscriptions. The Core has no listing endpoint; each recurring cycle's order names its
 * subscription, so the ids come from the order history and every subscription is then read from the Core.
 */
export async function listSubscriptionsFromOrders(orders: PaymentOrder[], signal?: AbortSignal): Promise<Subscription[]> {
  const ids = [...new Set(orders.map((order) => order.subscription_id).filter((id): id is string => Boolean(id)))]
  return Promise.all(ids.map((id) => getSubscription(id, signal)))
}

/** The provider is chosen server-side by the Payment Router when omitted. */
export function createCheckout(plan: string, idempotencyKey: string): Promise<CheckoutResponse> {
  return apiRequest<CheckoutResponse>('/billing/checkout', {
    method: 'POST',
    body: { plan, idempotency_key: idempotencyKey },
  })
}
