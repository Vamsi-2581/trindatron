import { act, fireEvent, screen, waitFor } from '@testing-library/react-native'
import { renderRouter } from 'expo-router/testing-library'
import { Alert, Text } from 'react-native'

import { useLocaleStore } from '@/i18n'
import { apiRequest } from '@/services/api/client'
import { isCommerciallyAvailable, listSellablePlans } from '@/services/api/billing'
import type { PaymentOrder, Subscription } from '@/services/api/types'
import { useAuthStore } from '@/stores/auth'

import BillingScreen from '../../app/(app)/billing'
import PlansScreen from '../../app/(app)/plans'

jest.mock('@/services/api/client', () => ({ ...jest.requireActual('@/services/api/client'), apiRequest: jest.fn() }))
const request = apiRequest as jest.MockedFunction<typeof apiRequest>

// What the Core's /billing/plans returns today: the retired flat TEAM is still in it.
const CORE_CATALOG = [
  { plan: 'start', amount: '9.90', currency: 'USD' },
  { plan: 'pro', amount: '19.90', currency: 'USD' },
  { plan: 'max', amount: '79.90', currency: 'USD' },
  { plan: 'team', amount: '199.90', currency: 'USD' },
]

const ORDER: PaymentOrder = {
  id: 'o1',
  user_id: 'u1',
  provider: 'stripe',
  external_order_id: null,
  status: 'confirmed',
  amount_fiat: '19.90',
  currency_fiat: 'USD',
  credits_amount: '0',
  currency_credits: 'TCU',
  idempotency_key: 'k',
  expires_at: '2026-10-01T00:00:00',
  confirmed_at: '2026-09-01T00:00:00',
  plan: 'pro',
  subscription_id: 's1',
  created_at: '2026-09-01T00:00:00',
  updated_at: '2026-09-01T00:00:00',
}

const SUBSCRIPTION: Subscription = {
  id: 's1',
  plan: 'pro',
  provider: 'stripe',
  payment_method: 'credit_card',
  payment_type: 'recurring',
  currency: 'USD',
  amount: '19.90',
  status: 'active',
  current_period_start: '2026-09-01T00:00:00',
  current_period_end: '2026-10-01T00:00:00',
  next_billing_at: '2026-10-01T00:00:00',
  cancelled_at: null,
  grace_period_ends_at: null,
  created_at: '2026-09-01T00:00:00',
  updated_at: '2026-09-01T00:00:00',
}

let orders: PaymentOrder[] = []
let subscription: Subscription = SUBSCRIPTION

beforeEach(() => {
  orders = []
  subscription = SUBSCRIPTION
  useLocaleStore.setState({ preference: 'auto', systemLocale: 'pt', locale: 'pt' })
  request.mockReset()
  request.mockImplementation(async (path: string, options?: { method?: string }) => {
    if (path === '/billing/plans') return CORE_CATALOG as never
    if (path === '/billing/providers') return { providers: [] } as never
    if (path.startsWith('/billing/orders')) return { orders, total: orders.length } as never
    if (path === '/billing/subscriptions/s1/cancel' && options?.method === 'POST') {
      subscription = { ...subscription, status: 'cancelled', cancelled_at: '2026-09-17T00:00:00' }
      return subscription as never
    }
    if (path === '/billing/subscriptions/s1') return subscription as never
    throw new Error(`unexpected ${path}`)
  })
  useAuthStore.setState({
    status: 'signedIn',
    token: 'tok',
    notice: null,
    user: { id: 'u1', email: 'ana@example.com', full_name: 'Ana', plan: 'start', is_active: true, timezone: null, created_at: '2026-09-13T10:00:00' },
  })
})

describe('TEAM pricing', () => {
  it('the flat TEAM entry is never a commercial offer', () => {
    expect(isCommerciallyAvailable({ plan: 'team', amount: '199.90', currency: 'USD' })).toBe(false)
    expect(isCommerciallyAvailable({ plan: 'pro', amount: '19.90', currency: 'USD' })).toBe(true)
  })

  it('prices come only from the Core catalog, minus the retired TEAM', async () => {
    const plans = await listSellablePlans()
    expect(plans.map((plan) => plan.plan)).toEqual(['start', 'pro', 'max'])
  })
})

describe('Plans screen', () => {
  it('shows each plan with the price and currency the Core returns, per month, and marks the current one', async () => {
    renderRouter({ plans: PlansScreen }, { initialUrl: '/plans' })

    expect(await screen.findByText('Pro')).toBeTruthy()
    expect(screen.getByText(/^US\$\s9,90 por mês$/)).toBeTruthy()
    expect(screen.getByText(/19,90 por mês/)).toBeTruthy()
    expect(screen.getByText(/79,90 por mês/)).toBeTruthy()
    expect(screen.getAllByText(/US\$/).length).toBeGreaterThanOrEqual(3)
    // START is this account's plan: no action to buy what it already has.
    expect(screen.getByText('Atual')).toBeTruthy()
    // Jest renders as iOS, where plans are shown but not sold in the app (App Store Review 3.1.1).
    expect(screen.getByText('Mudanças de plano não estão disponíveis no aplicativo para iOS.')).toBeTruthy()
    expect(screen.queryByText('Solicitar')).toBeNull()
    // The retired flat TEAM is never an offer.
    expect(screen.queryByText('Team')).toBeNull()
    expect(screen.queryByText(/199/)).toBeNull()
    expect(request).toHaveBeenCalledWith('/billing/plans', expect.anything())
  })
})

describe('Billing screen', () => {
  it('reads the subscription from the Core and cancels it there, never through support', async () => {
    orders = [ORDER]
    const alert = jest.spyOn(Alert, 'alert').mockImplementation((_title, _message, buttons) => buttons?.[1]?.onPress?.())
    renderRouter({ billing: BillingScreen, plans: () => <Text>plans-screen</Text>, 'support/index': () => <Text>support-screen</Text> }, {
      initialUrl: '/billing',
    })

    expect(await screen.findByText('Ativa')).toBeTruthy()
    expect(screen.getByText(/19,90 por mês · Cartão de crédito · próxima cobrança em/)).toBeTruthy()
    // Cancelling happens here, not by writing to Suporte.
    expect(screen.queryByText(/Ajuda e suporte|Suporte/)).toBeNull()

    fireEvent.press(screen.getByText('Cancelar assinatura'))
    await act(async () => {})
    expect(request).toHaveBeenCalledWith('/billing/subscriptions/s1/cancel', { method: 'POST' })
    expect(await screen.findByText('Assinatura cancelada.')).toBeTruthy()
    expect(await screen.findByText('Cancelada')).toBeTruthy()
    expect(screen.queryByText('Cancelar assinatura')).toBeNull()
    expect(screen).toHavePathname('/billing')
    alert.mockRestore()
  })

  it('says there is no subscription when no order names one, and opens the plans from here', async () => {
    renderRouter({ billing: BillingScreen, plans: () => <Text>plans-screen</Text> }, { initialUrl: '/billing' })

    expect(await screen.findByText('Nenhuma assinatura recorrente.')).toBeTruthy()
    expect(screen.getByText('Nenhum pagamento registrado.')).toBeTruthy()
    fireEvent.press(screen.getByText('Ver planos'))
    await waitFor(() => expect(screen).toHavePathname('/plans'))
  })
})
