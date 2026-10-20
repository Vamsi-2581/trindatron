'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { AlertTriangle } from 'lucide-react'
import { useAppStore } from '@/store/useAppStore'
import {
  apiGetCapacity,
  apiListMemories,
  apiCreateMemory,
  apiDeleteMemory,
  apiGetCreditSummary,
  apiListOrders,
} from '@/api/client'
import type { Capacity, Memory, CreditSummary, PaymentOrder } from '@/types'
import { ProgressBar } from '@/components/ui/progress-bar'
import { Badge } from '@/components/ui/badge'
import { Card } from '@/components/ui/card'

const PLAN_LABELS: Record<string, string> = {
  free: 'FREE', start: 'START', pro: 'PRO', max: 'MAX', team: 'TEAM',
}

const ORDER_STATUS_LABELS: Record<string, string> = {
  pending:       'Pendente',
  processing:    'Processando',
  confirmed:     'Confirmado',
  cancelled:     'Cancelado',
  failed:        'Falhou',
  expired:       'Expirado',
  refunded:      'Reembolsado',
  partial_refund:'Reembolso parcial',
}

const ORDER_STATUS_VARIANT: Record<string, 'accent' | 'muted' | 'error'> = {
  confirmed:     'accent',
  partial_refund:'accent',
  pending:       'muted',
  processing:    'muted',
  cancelled:     'muted',
  failed:        'error',
  expired:       'error',
  refunded:      'muted',
}

/** Percentage-only caption — Execution Runtime V1 (mission section 11):
 * the user never sees TCU/tokens/cost, only "X% usado" or "Sem limite". */
function pctUsedCaption(pctUsed: number | null): string {
  if (pctUsed === null) return 'Sem limite'
  return `${pctUsed.toFixed(0)}% usado`
}

function formatBalance(balance: string): string {
  const n = parseFloat(balance)
  return isNaN(n) ? balance : n.toFixed(2)
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString('pt-BR', { day: '2-digit', month: 'short', year: 'numeric' })
}

/** The backend sends next_reset_at as a UTC instant; formatting it with the
 * browser's own locale/timezone (no explicit timeZone override) shows it
 * correctly in the viewer's local time — it will always read as Sunday
 * 12:00 in whatever zone was detected and persisted for this user. */
function formatResetLabel(nextResetAtIso: string): string {
  const d = new Date(nextResetAtIso)
  const weekday = d.toLocaleDateString('pt-BR', { weekday: 'long' })
  const time = d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' })
  return `Reinicia ${weekday} às ${time}`
}

function formatCountdown(nextResetAtIso: string, nowMs: number): string {
  const diffMs = new Date(nextResetAtIso).getTime() - nowMs
  if (diffMs <= 0) return 'Atualizando…'
  const totalMinutes = Math.floor(diffMs / 60_000)
  const days = Math.floor(totalMinutes / (24 * 60))
  const hours = Math.floor((totalMinutes % (24 * 60)) / 60)
  const minutes = totalMinutes % 60
  if (days > 0) return `${days}d ${hours}h`
  if (hours > 0) return `${hours}h ${minutes}m`
  return `${minutes}m`
}

export default function AccountPage() {
  const { user } = useAppStore()
  const [capacity, setCapacity]               = useState<Capacity | null>(null)
  const [capacityLoading, setCapacityLoading] = useState(true)
  const [capacityError, setCapacityError]     = useState('')

  const [credits, setCredits]             = useState<CreditSummary | null>(null)
  const [creditsLoading, setCreditsLoading] = useState(true)

  const [orders, setOrders]             = useState<PaymentOrder[]>([])
  const [ordersLoading, setOrdersLoading] = useState(true)

  const [memories, setMemories]         = useState<Memory[]>([])
  const [memLoading, setMemLoading]     = useState(true)
  const [memError, setMemError]         = useState('')
  const [newMemory, setNewMemory]       = useState('')
  const [saving, setSaving]             = useState(false)
  const [deletingId, setDeletingId]     = useState<string | null>(null)

  const [nowMs, setNowMs] = useState(() => Date.now())

  useEffect(() => {
    // Local-only ticking clock for the weekly countdown — no network calls.
    const id = setInterval(() => setNowMs(Date.now()), 60_000)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    // Read-only — never starts/extends a session, never reserves anything;
    // see src/services/adaptive_budget_service.py.
    apiGetCapacity()
      .then(setCapacity)
      .catch(() => setCapacityError('Não foi possível carregar o uso'))
      .finally(() => setCapacityLoading(false))

    apiGetCreditSummary()
      .then(setCredits)
      .catch(() => { /* silently degrade — billing may be empty */ })
      .finally(() => setCreditsLoading(false))

    apiListOrders({ limit: 5 })
      .then(r => setOrders(r.orders))
      .catch(() => { /* silently degrade */ })
      .finally(() => setOrdersLoading(false))

    apiListMemories()
      .then(setMemories)
      .catch(() => setMemError('Não foi possível carregar as memórias'))
      .finally(() => setMemLoading(false))
  }, [])

  async function handleAddMemory() {
    const content = newMemory.trim()
    if (!content) return
    setSaving(true)
    setMemError('')
    try {
      const created = await apiCreateMemory(content)
      setMemories(prev => [created, ...prev])
      setNewMemory('')
    } catch (e) {
      setMemError(e instanceof Error ? e.message : 'Falha ao salvar memória')
    } finally {
      setSaving(false)
    }
  }

  async function handleDeleteMemory(id: string) {
    setDeletingId(id)
    try {
      await apiDeleteMemory(id)
      setMemories(prev => prev.filter(m => m.id !== id))
    } catch {
      setMemError('Falha ao remover memória')
    } finally {
      setDeletingId(null)
    }
  }

  const plan = user?.plan ?? 'free'
  const planLabel = PLAN_LABELS[plan] ?? plan.toUpperCase()

  // Weekly is the operational usage cap now — there is no monthly quota
  // (PARTE 10); the "limit reached" banner reflects the weekly window.
  const limitReached = (capacity?.weekly.pct_used ?? 0) >= 100

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-2xl mx-auto px-6 py-10">
        <h1 className="text-xl font-semibold text-text-primary mb-8">Minha conta</h1>

        {/* Limit reached banner */}
        {limitReached && (
          <div className="flex items-start gap-3 bg-error/10 border border-error/30 rounded-xl px-4 py-3 mb-5">
            <AlertTriangle className="w-4 h-4 text-error shrink-0 mt-0.5" />
            <div>
              <p className="text-sm font-medium text-error">Limite semanal atingido</p>
              <p className="text-xs text-text-muted mt-0.5">
                Você atingiu o limite de uso do seu plano.{' '}
                <Link href="/plans" className="text-accent-light hover:underline">
                  Faça upgrade
                </Link>{' '}
                para continuar.
              </p>
            </div>
          </div>
        )}

        {/* Profile */}
        <Card className="mb-5">
          <Card.Header>
            <span className="text-xs font-semibold text-text-muted uppercase tracking-wider">Perfil</span>
          </Card.Header>
          <Card.Body>
            <div className="flex items-center gap-4">
              <div className="w-12 h-12 rounded-full bg-accent/20 flex items-center justify-center shrink-0">
                <span className="text-lg font-semibold text-accent-light">
                  {user?.email?.[0]?.toUpperCase() ?? 'U'}
                </span>
              </div>
              <div>
                <p className="text-sm text-text-primary">{user?.email}</p>
                <div className="flex items-center gap-2 mt-1">
                  <Badge variant="accent">{planLabel}</Badge>
                </div>
              </div>
            </div>
          </Card.Body>
        </Card>

        {/* Usage — limites de uso do plano. Execution Runtime V1 (mission
            section 11): percentuais e status apenas — nunca TCU/tokens/custo.
            Ver GET /users/me/capacity e adaptive_budget_service.py. */}
        <Card className="mb-5">
          <Card.Header>
            <span className="text-xs font-semibold text-text-muted uppercase tracking-wider">Limites de uso</span>
          </Card.Header>
          <Card.Body className="space-y-5">
            {capacityLoading && <p className="text-sm text-text-muted">Carregando…</p>}
            {capacityError && <p className="text-sm text-error">{capacityError}</p>}
            {capacity && !capacityLoading && (
              <>
                <div>
                  <ProgressBar
                    value={capacity.session.pct_used ?? 0}
                    label="Sessão atual"
                    caption={pctUsedCaption(capacity.session.pct_used)}
                  />
                  <p className="text-xs text-text-muted mt-1.5">
                    {capacity.session.active && capacity.session.resets_at ? (
                      <span className="tabular-nums">
                        Reinicia em {formatCountdown(capacity.session.resets_at, nowMs)}
                      </span>
                    ) : (
                      'Nenhuma sessão ativa no momento.'
                    )}
                  </p>
                </div>
                <div>
                  <ProgressBar
                    value={capacity.weekly.pct_used ?? 0}
                    label="Limite semanal"
                    caption={pctUsedCaption(capacity.weekly.pct_used)}
                  />
                  {capacity.weekly.resets_at && (
                    <p className="text-xs text-text-muted mt-1.5">
                      {formatResetLabel(capacity.weekly.resets_at)}
                      {' · '}
                      <span className="tabular-nums">{formatCountdown(capacity.weekly.resets_at, nowMs)}</span>
                    </p>
                  )}
                </div>
                <div className="flex items-center justify-between pt-1 border-t border-border">
                  <div>
                    <p className="text-xs text-text-muted">Agentes ativos</p>
                    <p className="text-lg font-semibold text-text-primary tabular-nums">{capacity.active_agents}</p>
                  </div>
                  <div className="text-right">
                    <p className="text-xs text-text-muted">Capacidade restante</p>
                    <p className="text-lg font-semibold text-text-primary tabular-nums">
                      {capacity.capacity_remaining_pct.toFixed(0)}%
                    </p>
                  </div>
                </div>
                {capacity.optimizing && (
                  <p className="text-xs text-accent-light">
                    O sistema está otimizando o consumo para manter suas tarefas mais importantes funcionando.
                  </p>
                )}
              </>
            )}
          </Card.Body>
        </Card>

        {/* Credit balance */}
        <Card className="mb-5">
          <Card.Header>
            <span className="text-xs font-semibold text-text-muted uppercase tracking-wider">Saldo de créditos</span>
          </Card.Header>
          <Card.Body>
            {creditsLoading ? (
              <p className="text-sm text-text-muted">Carregando…</p>
            ) : !credits || credits.balances.length === 0 ? (
              <p className="text-sm text-text-muted italic">Nenhum saldo disponível.</p>
            ) : (
              <ul className="space-y-2">
                {credits.balances.map(b => (
                  <li key={b.currency} className="flex items-center justify-between">
                    <span className="text-xs font-medium text-text-muted uppercase tracking-wider">
                      {b.currency}
                    </span>
                    <span className="text-sm font-semibold text-text-primary tabular-nums">
                      {formatBalance(b.balance)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card.Body>
        </Card>

        {/* Recent orders */}
        {!ordersLoading && orders.length > 0 && (
          <Card className="mb-5">
            <Card.Header>
              <span className="text-xs font-semibold text-text-muted uppercase tracking-wider">Histórico de compras</span>
            </Card.Header>
            <Card.Body className="space-y-2">
              {orders.map(o => (
                <div
                  key={o.id}
                  className="flex items-center justify-between gap-3 py-1.5 border-b border-border last:border-0"
                >
                  <div className="min-w-0">
                    <p className="text-xs text-text-muted truncate">{formatDate(o.created_at)}</p>
                    <p className="text-sm text-text-primary font-medium tabular-nums">
                      {o.amount_fiat} {o.currency_fiat}
                      <span className="text-xs text-text-muted font-normal ml-1">
                        → {o.credits_amount} {o.currency_credits}
                      </span>
                    </p>
                  </div>
                  <Badge variant={ORDER_STATUS_VARIANT[o.status] ?? 'muted'}>
                    {ORDER_STATUS_LABELS[o.status] ?? o.status}
                  </Badge>
                </div>
              ))}
            </Card.Body>
          </Card>
        )}

        {/* Subscription */}
        <Card className="mb-5">
          <Card.Header>
            <span className="text-xs font-semibold text-text-muted uppercase tracking-wider">Assinatura</span>
          </Card.Header>
          <Card.Body>
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-text-primary font-medium">Plano {planLabel}</p>
                <p className="text-xs text-text-muted mt-0.5">
                  {limitReached
                    ? 'Limite atingido — considere fazer upgrade'
                    : 'Gerencie seu plano para aumentar seus limites'}
                </p>
              </div>
              <Link
                href="/plans"
                className="text-xs text-accent-light hover:text-accent transition-colors whitespace-nowrap"
              >
                {limitReached ? 'Fazer upgrade →' : 'Ver planos →'}
              </Link>
            </div>
          </Card.Body>
        </Card>

        {/* Memories */}
        <Card>
          <Card.Header>
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-text-muted uppercase tracking-wider">Memórias</span>
              <Link href="/memory" className="text-xs text-accent-light hover:underline">
                Ver todas →
              </Link>
            </div>
          </Card.Header>
          <Card.Body className="space-y-4">
            <p className="text-xs text-text-muted">
              Fatos que o TRINDATRON lembrará em todas as conversas futuras.
            </p>
            <div className="flex gap-2">
              <input
                type="text"
                value={newMemory}
                onChange={e => setNewMemory(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && !saving && handleAddMemory()}
                placeholder="Ex: Prefiro respostas em português"
                maxLength={2000}
                disabled={saving}
                className="flex-1 bg-surface-2 border border-border rounded-lg px-3 py-2 text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent/60 disabled:opacity-50"
              />
              <button
                onClick={handleAddMemory}
                disabled={saving || !newMemory.trim()}
                className="px-4 py-2 text-xs font-medium bg-accent text-text-inverse rounded-lg hover:bg-accent/90 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                {saving ? 'Salvando…' : 'Salvar'}
              </button>
            </div>
            {memError && <p className="text-xs text-error">{memError}</p>}
            {memLoading ? (
              <p className="text-xs text-text-muted">Carregando…</p>
            ) : memories.length === 0 ? (
              <p className="text-xs text-text-muted italic">Nenhuma memória salva ainda.</p>
            ) : (
              <ul className="space-y-2">
                {memories.map(m => (
                  <li
                    key={m.id}
                    className="flex items-start justify-between gap-3 bg-surface-2 rounded-lg px-3 py-2.5"
                  >
                    <p className="text-sm text-text-primary leading-snug flex-1">{m.content}</p>
                    <button
                      onClick={() => handleDeleteMemory(m.id)}
                      disabled={deletingId === m.id}
                      aria-label="Remover memória"
                      className="shrink-0 text-text-muted hover:text-error transition-colors disabled:opacity-40 mt-0.5"
                    >
                      {deletingId === m.id ? '…' : '×'}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card.Body>
        </Card>
      </div>
    </div>
  )
}
