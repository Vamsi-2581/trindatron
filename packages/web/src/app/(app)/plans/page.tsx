'use client'

import { useEffect, useState } from 'react'
import { CheckCircle2, ArrowUpRight, X, Loader2, AlertCircle } from 'lucide-react'
import { useAppStore } from '@/store/useAppStore'
import { Badge } from '@/components/ui/badge'
import {
  apiListPlanPrices, apiListPaymentProviders, apiCreateCheckout, apiGetOrder,
  apiGetMe, getStoredToken,
} from '@/api/client'
import type { PlanPrice, PaymentOrder } from '@/types'

// Mirror of backend PlanEntitlement — display only, not enforced here.
//
// Described by what each plan lets you do, not by TCU. Two reasons:
// TCU is this platform's internal accounting unit and means nothing to
// someone choosing a plan, and the monthly figures this list used to quote
// ("100 TCU/mês", "100.000 TCU/mês") were never real — monthly_tcu_limit is
// None for EVERY plan in src/entitlements.py, so the page was inventing a
// cap the backend does not enforce. The daily limits ARE real (10 / 100 /
// 1.000, and none for MAX/TEAM) and are expressed here as relative volume.
const PLANS = [
  {
    id: 'free',
    name: 'FREE',
    tagline: 'Para experimentar',
    limits: 'Uso leve, para conhecer',
    features: [
      'Conversa, pesquisa e memória',
      'Até 3 projetos',
      'Histórico das suas sessões',
      'Suporte por email',
    ],
  },
  {
    id: 'start',
    name: 'START',
    tagline: 'Para começar a trabalhar',
    limits: 'Uso diário',
    features: [
      'Agentes que executam tarefas',
      'Navegador, arquivos e terminal',
      'Até 10 projetos e 5 agentes',
      'Análise e síntese avançada',
      'Suporte prioritário',
    ],
  },
  {
    id: 'pro',
    name: 'PRO',
    tagline: 'Para produtividade profissional',
    limits: 'Uso profissional, todo dia',
    features: [
      'Tudo do START',
      'Execução com aprovação (WORK)',
      'Até 50 projetos e 20 agentes',
      'Muito mais volume de execução',
      'Suporte dedicado',
    ],
    highlight: true,
  },
  {
    id: 'max',
    name: 'MAX',
    tagline: 'Para uso intensivo',
    limits: 'Uso intenso, o dia inteiro',
    features: [
      'Tudo do PRO, com muito mais uso',
      'Acesso às inteligências premium',
      'Sessões que renovam a cada 5 horas',
      'Projetos e agentes sem limite fixo',
      'Tarefas mais longas por pedido',
    ],
  },
  {
    id: 'team',
    name: 'TEAM',
    tagline: 'Para equipes e empresas',
    limits: 'Uso intenso, compartilhado',
    features: [
      'Tudo do MAX, para a equipe',
      'Uso compartilhado com controle',
      'Workspace centralizado',
      'Onboarding dedicado',
      'Contrato empresarial',
    ],
  },
]

type UpgradeState = 'idle' | 'contact' | 'sent' | 'checkout' | 'pending' | 'success' | 'error'

// Checkout order polling — how long to keep checking before telling the
// user it's taking a while (the order itself keeps confirming later via
// the real webhook whenever the provider notifies; this is purely about
// how long THIS tab keeps watching for it).
const POLL_INTERVAL_MS = 3000
const POLL_MAX_ATTEMPTS = 40 // ~2 minutes

function formatPrice(price: PlanPrice | undefined): string {
  if (!price) return ''
  const amount = Number(price.amount)
  return `${price.currency} ${amount.toFixed(2)}/mês`
}

function checkoutStorageKey(plan: string): string {
  return `trinity_checkout_idempotency_key:${plan}`
}

function getOrCreateIdempotencyKey(plan: string): string {
  const key = checkoutStorageKey(plan)
  const existing = typeof window !== 'undefined' ? sessionStorage.getItem(key) : null
  if (existing) return existing
  const fresh = crypto.randomUUID()
  if (typeof window !== 'undefined') sessionStorage.setItem(key, fresh)
  return fresh
}

function clearIdempotencyKey(plan: string): void {
  if (typeof window !== 'undefined') sessionStorage.removeItem(checkoutStorageKey(plan))
}

export default function PlansPage() {
  const { user, setAuth } = useAppStore()
  const currentPlan = user?.plan ?? 'free'

  const [prices, setPrices] = useState<PlanPrice[]>([])
  const [providers, setProviders] = useState<string[]>([])
  const [loadingCatalog, setLoadingCatalog] = useState(true)

  const [upgradeTarget, setUpgradeTarget] = useState<string | null>(null)
  const [upgradeState, setUpgradeState] = useState<UpgradeState>('idle')
  const [checkoutError, setCheckoutError] = useState('')
  const [pendingOrder, setPendingOrder] = useState<PaymentOrder | null>(null)

  useEffect(() => {
    Promise.all([apiListPlanPrices(), apiListPaymentProviders()])
      .then(([priceList, providerList]) => {
        setPrices(priceList)
        setProviders(providerList.providers)
      })
      .catch(() => {
        // Silent — the page still works with the mailto fallback if the
        // catalog fails to load; checkoutAvailable simply stays false.
      })
      .finally(() => setLoadingCatalog(false))
  }, [])

  const checkoutAvailable = providers.length > 0
  const priceFor = (planId: string) => prices.find((p) => p.plan === planId)

  function handleSelect(planId: string) {
    setUpgradeTarget(planId)
    setCheckoutError('')
    setPendingOrder(null)
    setUpgradeState(checkoutAvailable && priceFor(planId) ? 'checkout' : 'contact')
  }

  function handleDismiss() {
    setUpgradeTarget(null)
    setUpgradeState('idle')
    setCheckoutError('')
    setPendingOrder(null)
  }

  function handleContact() {
    const plan = PLANS.find(p => p.id === upgradeTarget)
    const subject = encodeURIComponent(`Upgrade para plano ${plan?.name ?? upgradeTarget}`)
    const body = encodeURIComponent(
      `Olá! Tenho interesse em fazer upgrade para o plano ${plan?.name ?? upgradeTarget}.\n\nMeu email: ${user?.email ?? ''}\n`,
    )
    window.open(`mailto:contato@trinity.ai?subject=${subject}&body=${body}`, '_blank')
    setUpgradeState('sent')
  }

  async function pollOrderUntilConfirmed(orderId: string, plan: string) {
    for (let attempt = 0; attempt < POLL_MAX_ATTEMPTS; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS))
      try {
        const order = await apiGetOrder(orderId)
        setPendingOrder(order)
        if (order.status === 'confirmed') {
          clearIdempotencyKey(plan)
          const token = getStoredToken()
          if (token) {
            const freshUser = await apiGetMe()
            setAuth(token, freshUser)
          }
          setUpgradeState('success')
          return
        }
        if (['failed', 'cancelled', 'expired'].includes(order.status)) {
          setCheckoutError('O pagamento não foi concluído. Tente novamente.')
          setUpgradeState('error')
          return
        }
      } catch {
        // transient — keep polling
      }
    }
    // Still pending after the polling window — not an error, just slow.
    // The order stays valid and will confirm on its own once the
    // provider's webhook arrives; nothing to lose by leaving it open.
  }

  async function handlePay() {
    if (!upgradeTarget) return
    setCheckoutError('')
    setUpgradeState('pending')
    try {
      const provider = providers[0]
      const idempotencyKey = getOrCreateIdempotencyKey(upgradeTarget)
      const result = await apiCreateCheckout(upgradeTarget, provider, idempotencyKey)
      setPendingOrder(result.order)

      if (result.redirect_url) {
        window.location.href = result.redirect_url
        return
      }

      if (result.order.status === 'confirmed') {
        clearIdempotencyKey(upgradeTarget)
        const token = getStoredToken()
        if (token) {
          const freshUser = await apiGetMe()
          setAuth(token, freshUser)
        }
        setUpgradeState('success')
        return
      }

      pollOrderUntilConfirmed(result.order.id, upgradeTarget)
    } catch (err) {
      setCheckoutError(err instanceof Error ? err.message : 'Não foi possível iniciar o checkout')
      setUpgradeState('error')
    }
  }

  const targetPlan = PLANS.find(p => p.id === upgradeTarget)
  const targetPrice = upgradeTarget ? priceFor(upgradeTarget) : undefined

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-4xl mx-auto px-6 py-10">
        <div className="mb-10">
          <h1 className="text-xl font-semibold text-text-primary mb-2">Planos</h1>
          <p className="text-sm text-text-muted">
            Escolha o plano que corresponde ao seu volume de trabalho.
          </p>
        </div>

        {/* Upgrade feedback panel */}
        {upgradeState !== 'idle' && targetPlan && (
          <div className="relative bg-surface-2 border border-accent/30 rounded-2xl p-5 mb-8">
            <button
              onClick={handleDismiss}
              className="absolute top-4 right-4 text-text-muted hover:text-text-primary transition-colors"
              aria-label="Fechar"
            >
              <X className="w-4 h-4" />
            </button>

            {upgradeState === 'contact' && (
              <>
                <p className="text-sm font-medium text-text-primary mb-1">
                  Upgrade para <span className="text-accent-light">{targetPlan.name}</span>
                </p>
                <p className="text-xs text-text-muted mb-4">
                  Os upgrades são processados manualmente pela nossa equipe.
                  Clique abaixo para enviar sua solicitação por email.
                </p>
                <button
                  onClick={handleContact}
                  className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-accent text-text-inverse text-sm font-medium hover:bg-accent/90 transition-colors"
                >
                  Enviar solicitação
                  <ArrowUpRight className="w-3.5 h-3.5" />
                </button>
              </>
            )}

            {upgradeState === 'sent' && (
              <>
                <div className="flex items-center gap-2 mb-1">
                  <CheckCircle2 className="w-4 h-4 text-success" />
                  <p className="text-sm font-medium text-text-primary">Solicitação enviada!</p>
                </div>
                <p className="text-xs text-text-muted">
                  Nossa equipe entrará em contato em até 24 horas para confirmar o upgrade para{' '}
                  <strong>{targetPlan.name}</strong>.
                </p>
              </>
            )}

            {upgradeState === 'checkout' && (
              <>
                <p className="text-sm font-medium text-text-primary mb-1">
                  Upgrade para <span className="text-accent-light">{targetPlan.name}</span>
                </p>
                <p className="text-xs text-text-muted mb-1">{targetPlan.tagline}</p>
                <p className="text-lg font-semibold text-text-primary mt-3 mb-4">
                  {formatPrice(targetPrice)}
                </p>
                <button
                  onClick={handlePay}
                  className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-accent text-text-inverse text-sm font-medium hover:bg-accent/90 transition-colors"
                >
                  Pagar agora
                  <ArrowUpRight className="w-3.5 h-3.5" />
                </button>
              </>
            )}

            {upgradeState === 'pending' && (
              <>
                <div className="flex items-center gap-2 mb-1">
                  <Loader2 className="w-4 h-4 text-accent-light animate-spin" />
                  <p className="text-sm font-medium text-text-primary">
                    {pendingOrder ? 'Aguardando confirmação do pagamento…' : 'Iniciando checkout…'}
                  </p>
                </div>
                <p className="text-xs text-text-muted">
                  {pendingOrder
                    ? 'Isso pode levar alguns instantes. Você pode fechar esta janela — o upgrade é confirmado automaticamente assim que o pagamento for aprovado.'
                    : 'Só um momento.'}
                </p>
              </>
            )}

            {upgradeState === 'success' && (
              <>
                <div className="flex items-center gap-2 mb-1">
                  <CheckCircle2 className="w-4 h-4 text-success" />
                  <p className="text-sm font-medium text-text-primary">Upgrade confirmado!</p>
                </div>
                <p className="text-xs text-text-muted">
                  Você já está no plano <strong>{targetPlan.name}</strong>. Aproveite!
                </p>
              </>
            )}

            {upgradeState === 'error' && (
              <>
                <div className="flex items-center gap-2 mb-1">
                  <AlertCircle className="w-4 h-4 text-error" />
                  <p className="text-sm font-medium text-text-primary">Não foi possível concluir</p>
                </div>
                <p className="text-xs text-text-muted mb-4">
                  {checkoutError || 'Ocorreu um erro ao processar seu pagamento.'}
                </p>
                <button
                  onClick={() => handleSelect(targetPlan.id)}
                  className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-accent text-text-inverse text-sm font-medium hover:bg-accent/90 transition-colors"
                >
                  Tentar novamente
                </button>
              </>
            )}
          </div>
        )}

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {PLANS.map((plan) => {
            const isCurrent = plan.id === currentPlan
            const isHighlight = plan.highlight && !isCurrent
            const isDowngrade = PLANS.findIndex(p => p.id === plan.id) < PLANS.findIndex(p => p.id === currentPlan)
            const price = priceFor(plan.id)

            return (
              <div
                key={plan.id}
                className={[
                  'flex flex-col rounded-2xl border p-5 transition-all',
                  isCurrent
                    ? 'bg-accent/5 border-accent/30'
                    : isHighlight
                    ? 'bg-surface-2 border-border'
                    : 'bg-surface-1 border-border',
                ].join(' ')}
              >
                <div className="flex items-start justify-between mb-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-semibold text-text-primary tracking-wide">
                      {plan.name}
                    </span>
                    {isCurrent && <Badge variant="accent">Atual</Badge>}
                    {isHighlight && <Badge variant="muted">Popular</Badge>}
                  </div>
                </div>

                <p className="text-xs text-text-muted mb-1">{plan.tagline}</p>

                {checkoutAvailable && price && (
                  <p className="text-sm font-semibold text-text-primary mb-1">{formatPrice(price)}</p>
                )}

                {/* Volume of use, in plain language */}
                <p className="text-xs text-accent-light/80 font-medium mb-4">{plan.limits}</p>

                <ul className="flex-1 space-y-2 mb-5">
                  {plan.features.map((f) => (
                    <li key={f} className="flex items-start gap-2 text-xs text-text-secondary">
                      <CheckCircle2 className="w-3.5 h-3.5 text-success shrink-0 mt-0.5" />
                      <span>{f}</span>
                    </li>
                  ))}
                </ul>

                <button
                  type="button"
                  onClick={() => !isCurrent && !isDowngrade && handleSelect(plan.id)}
                  disabled={isCurrent || loadingCatalog}
                  className={[
                    'w-full py-2 rounded-xl text-sm font-medium transition-all',
                    isCurrent
                      ? 'bg-accent/10 text-accent-light cursor-default'
                      : isDowngrade
                      ? 'bg-surface-3 text-text-muted cursor-not-allowed opacity-50'
                      : upgradeTarget === plan.id
                      ? 'bg-accent text-text-inverse'
                      : 'bg-surface-3 text-text-secondary hover:bg-accent hover:text-text-inverse',
                  ].join(' ')}
                >
                  {isCurrent
                    ? 'Plano atual'
                    : isDowngrade
                    ? 'Plano inferior'
                    : upgradeTarget === plan.id && upgradeState === 'sent'
                    ? '✓ Solicitado'
                    : 'Fazer upgrade'}
                </button>
              </div>
            )
          })}
        </div>

        {!checkoutAvailable && (
          <p className="text-center text-xs text-text-muted mt-8">
            Pagamento automatizado ainda não está disponível nesta conta.
            Para upgrades imediatos,{' '}
            <a
              href="mailto:contato@trinity.ai"
              className="text-accent-light hover:underline"
            >
              entre em contato
            </a>.
          </p>
        )}
      </div>
    </div>
  )
}
