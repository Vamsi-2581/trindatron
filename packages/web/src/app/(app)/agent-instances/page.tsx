'use client'

import { useEffect, useState } from 'react'
import { Activity, Plus, X, Play, Pause, PlayCircle } from 'lucide-react'
import {
  apiListAgentInstances, apiCreateAgentInstance, apiPauseAgentInstance,
  apiResumeAgentInstance, apiRunAgentInstance, apiListAgents,
} from '@/api/client'
import type { AgentInstance, AgentDefinition } from '@/types'

function formatDateTime(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('pt-BR', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
}

const STATUS_LABELS: Record<string, string> = {
  active: 'Ativo', paused: 'Pausado', disabled: 'Desativado', error: 'Erro',
}

const HEALTH_STYLES: Record<string, string> = {
  healthy: 'bg-success/10 text-success',
  degraded: 'bg-warning/10 text-warning',
  failed: 'bg-error/10 text-error',
}

const HEALTH_LABELS: Record<string, string> = {
  healthy: 'Saudável', degraded: 'Degradado', failed: 'Falhou',
}

const DECISION_LABELS: Record<string, string> = {
  acted: 'Agiu', skipped: 'Sem mudanças', capacity_refused: 'Capacidade insuficiente',
  decide_pending: 'Decidindo…', decide_failed: 'Falha ao decidir', decide_invalid: 'Decisão inválida',
  decide_skip: 'Decidiu não agir',
}

export default function AgentInstancesPage() {
  const [instances, setInstances] = useState<AgentInstance[]>([])
  const [definitions, setDefinitions] = useState<AgentDefinition[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busyId, setBusyId] = useState<string | null>(null)

  const [showModal, setShowModal] = useState(false)
  const [creating, setCreating] = useState(false)
  const [newDefinitionId, setNewDefinitionId] = useState('')
  const [newName, setNewName] = useState('')

  function load() {
    setLoading(true)
    Promise.all([apiListAgentInstances(), apiListAgents()])
      .then(([instancesRes, definitionsRes]) => {
        setInstances(instancesRes.instances)
        setDefinitions(definitionsRes.agents)
      })
      .catch(() => setError('Não foi possível carregar os agentes persistentes'))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  function definitionName(agentDefinitionId: string): string {
    return definitions.find((d) => d.id === agentDefinitionId)?.name ?? 'Agente removido'
  }

  async function handleCreate() {
    if (!newDefinitionId || !newName.trim() || creating) return
    setCreating(true)
    try {
      const instance = await apiCreateAgentInstance(newDefinitionId, newName.trim())
      setInstances((prev) => [instance, ...prev])
      setShowModal(false)
      setNewDefinitionId('')
      setNewName('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Não foi possível criar o agente persistente')
    } finally {
      setCreating(false)
    }
  }

  async function handleToggle(instance: AgentInstance) {
    setBusyId(instance.id)
    setError('')
    try {
      const updated = instance.status === 'active'
        ? await apiPauseAgentInstance(instance.id)
        : await apiResumeAgentInstance(instance.id)
      setInstances((prev) => prev.map((i) => (i.id === updated.id ? updated : i)))
    } catch {
      setError('Não foi possível atualizar o status do agente')
    } finally {
      setBusyId(null)
    }
  }

  async function handleRunNow(instance: AgentInstance) {
    setBusyId(instance.id)
    setError('')
    try {
      await apiRunAgentInstance(instance.id)
      const refreshed = await apiListAgentInstances()
      setInstances(refreshed.instances)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Não foi possível executar o agente agora')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-4xl mx-auto px-6 py-10">
        <div className="flex items-center justify-between mb-8">
          <div>
            <h1 className="text-xl font-semibold text-text-primary mb-1">Agentes Persistentes</h1>
            <p className="text-sm text-text-muted">
              Agentes com estado e ciclo de vida próprios, monitorando e agindo em segundo plano.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setShowModal(true)}
            disabled={definitions.length === 0}
            className="flex items-center gap-2 px-4 py-2 bg-accent hover:bg-accent-hover text-text-inverse text-sm font-medium rounded-xl transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Plus className="w-4 h-4" />
            Nova instância
          </button>
        </div>

        {error && <p className="text-sm text-error mb-6">{error}</p>}

        {loading ? (
          <div className="flex flex-col gap-3">
            {[1, 2].map((i) => (
              <div key={i} className="h-24 bg-surface-1 border border-border rounded-xl animate-pulse" />
            ))}
          </div>
        ) : instances.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-24 text-center">
            <div className="w-16 h-16 rounded-2xl bg-surface-2 border border-border flex items-center justify-center mb-4">
              <Activity className="w-7 h-7 text-text-muted" />
            </div>
            <p className="text-sm font-medium text-text-secondary mb-1">Nenhuma instância ainda</p>
            <p className="text-xs text-text-muted max-w-sm leading-relaxed mb-5">
              Crie uma instância a partir de um agente para que ele monitore, decida e aja sozinho em um intervalo.
            </p>
            <button
              type="button"
              onClick={() => setShowModal(true)}
              disabled={definitions.length === 0}
              className="flex items-center gap-2 px-4 py-2 bg-accent hover:bg-accent-hover text-text-inverse text-sm font-medium rounded-xl transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <Plus className="w-4 h-4" />
              Criar instância
            </button>
            {definitions.length === 0 && (
              <p className="text-xs text-text-muted mt-3">Crie um agente em /agents primeiro.</p>
            )}
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {instances.map((instance) => (
              <div key={instance.id} className="flex items-start gap-4 p-5 bg-surface-1 border border-border rounded-xl">
                <div className="w-10 h-10 rounded-xl bg-surface-2 border border-border flex items-center justify-center shrink-0">
                  <Activity className="w-5 h-5 text-text-muted" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap mb-0.5">
                    <p className="text-sm font-medium text-text-primary">{instance.name}</p>
                    <span className="text-[11px] px-2 py-0.5 rounded-full bg-surface-2 text-text-muted">
                      {STATUS_LABELS[instance.status] ?? instance.status}
                    </span>
                    <span className={`text-[11px] px-2 py-0.5 rounded-full ${HEALTH_STYLES[instance.health] ?? 'bg-surface-2 text-text-muted'}`}>
                      {HEALTH_LABELS[instance.health] ?? instance.health}
                    </span>
                  </div>
                  <p className="text-xs text-text-muted truncate mb-1">{definitionName(instance.agent_definition_id)}</p>
                  <p className="text-[11px] text-text-muted">
                    Última execução: {formatDateTime(instance.last_run_at)} · Próxima: {formatDateTime(instance.next_run_at)}
                    {instance.last_decision && (
                      <> · Última decisão: {DECISION_LABELS[instance.last_decision] ?? instance.last_decision}</>
                    )}
                  </p>
                  {instance.last_error && instance.health !== 'healthy' && (
                    <p className="text-[11px] text-error mt-1 truncate">{instance.last_error}</p>
                  )}
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <button
                    type="button"
                    onClick={() => handleRunNow(instance)}
                    disabled={busyId === instance.id || instance.status !== 'active'}
                    title="Executar agora"
                    className="p-2 rounded-lg text-text-muted hover:text-text-primary hover:bg-surface-3 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    <PlayCircle className="w-4 h-4" />
                  </button>
                  <button
                    type="button"
                    onClick={() => handleToggle(instance)}
                    disabled={busyId === instance.id || (instance.status !== 'active' && instance.status !== 'paused')}
                    title={instance.status === 'active' ? 'Pausar' : 'Retomar'}
                    className="p-2 rounded-lg text-text-muted hover:text-text-primary hover:bg-surface-3 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {instance.status === 'active' ? <Pause className="w-4 h-4" /> : <Play className="w-4 h-4" />}
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {showModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm">
          <div className="w-full max-w-md bg-surface-1 border border-border rounded-2xl shadow-xl p-6">
            <div className="flex items-center justify-between mb-5">
              <h2 className="text-sm font-semibold text-text-primary">Nova instância</h2>
              <button
                type="button"
                onClick={() => { setShowModal(false); setNewDefinitionId(''); setNewName('') }}
                className="p-1 rounded-lg text-text-muted hover:text-text-primary hover:bg-surface-3 transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="space-y-4">
              <div>
                <label className="block text-xs font-medium text-text-secondary mb-1.5">Agente</label>
                <select
                  value={newDefinitionId}
                  onChange={(e) => setNewDefinitionId(e.target.value)}
                  className="w-full bg-surface-2 border border-border rounded-xl px-4 py-2.5 text-sm text-text-primary outline-none focus:border-accent/50 transition-colors"
                >
                  <option value="">Selecione um agente…</option>
                  {definitions.map((d) => (
                    <option key={d.id} value={d.id}>{d.name}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-xs font-medium text-text-secondary mb-1.5">Nome da instância</label>
                <input
                  type="text"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
                  placeholder="Ex: Monitor do site principal"
                  autoFocus
                  className="w-full bg-surface-2 border border-border rounded-xl px-4 py-2.5 text-sm text-text-primary placeholder:text-text-muted outline-none focus:border-accent/50 transition-colors"
                />
              </div>
            </div>

            <div className="flex gap-3 mt-6">
              <button
                type="button"
                onClick={() => { setShowModal(false); setNewDefinitionId(''); setNewName('') }}
                className="flex-1 py-2.5 rounded-xl text-sm text-text-secondary bg-surface-2 hover:bg-surface-3 transition-colors"
              >
                Cancelar
              </button>
              <button
                type="button"
                onClick={handleCreate}
                disabled={!newDefinitionId || !newName.trim() || creating}
                className="flex-1 py-2.5 rounded-xl text-sm font-medium text-text-inverse bg-accent hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {creating ? 'Criando…' : 'Criar instância'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
