'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Bot, Plus, X } from 'lucide-react'
import { useAppStore } from '@/store/useAppStore'
import { apiListAgents, apiCreateAgent } from '@/api/client'

export default function AgentsPage() {
  const router = useRouter()
  const { agentDefinitions, setAgentDefinitions, prependAgentDefinition } = useAppStore()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [showModal, setShowModal] = useState(false)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const [newEmoji, setNewEmoji] = useState('')

  useEffect(() => {
    apiListAgents()
      .then((data) => setAgentDefinitions(data.agents))
      .catch(() => setError('Não foi possível carregar os agentes'))
      .finally(() => setLoading(false))
  }, [setAgentDefinitions])

  async function handleCreate() {
    if (!newName.trim() || creating) return
    setCreating(true)
    try {
      const agent = await apiCreateAgent(newName.trim(), {
        description: newDesc.trim() || undefined,
        avatar_emoji: newEmoji.trim() || undefined,
      })
      prependAgentDefinition(agent)
      setShowModal(false)
      setNewName('')
      setNewDesc('')
      setNewEmoji('')
      router.push(`/agents/${agent.id}`)
    } catch {
      setError('Não foi possível criar o agente')
      setCreating(false)
    }
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-3xl mx-auto px-6 py-10">
        <div className="flex items-center justify-between mb-8">
          <div>
            <h1 className="text-xl font-semibold text-text-primary mb-1">Meus Agentes</h1>
            <p className="text-sm text-text-muted">
              Crie agentes personalizados com instruções e skills dedicadas.
            </p>
          </div>
          <button
            type="button"
            onClick={() => setShowModal(true)}
            className="flex items-center gap-2 px-4 py-2 bg-accent hover:bg-accent-hover text-text-inverse text-sm font-medium rounded-xl transition-colors"
          >
            <Plus className="w-4 h-4" />
            Novo agente
          </button>
        </div>

        {error && <p className="text-sm text-error mb-6">{error}</p>}

        {loading ? (
          <div className="flex flex-col gap-3">
            {[1, 2].map((i) => (
              <div key={i} className="h-20 bg-surface-1 border border-border rounded-xl animate-pulse" />
            ))}
          </div>
        ) : agentDefinitions.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-24 text-center">
            <div className="w-16 h-16 rounded-2xl bg-surface-2 border border-border flex items-center justify-center mb-4">
              <Bot className="w-7 h-7 text-text-muted" />
            </div>
            <p className="text-sm font-medium text-text-secondary mb-1">Nenhum agente ainda</p>
            <p className="text-xs text-text-muted max-w-xs leading-relaxed mb-5">
              Crie agentes especializados com skills e instruções personalizadas para cada tipo de tarefa.
            </p>
            <button
              type="button"
              onClick={() => setShowModal(true)}
              className="flex items-center gap-2 px-4 py-2 bg-accent hover:bg-accent-hover text-text-inverse text-sm font-medium rounded-xl transition-colors"
            >
              <Plus className="w-4 h-4" />
              Criar agente
            </button>
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {agentDefinitions.map((a) => (
              <button
                key={a.id}
                type="button"
                onClick={() => router.push(`/agents/${a.id}`)}
                className="flex items-start gap-4 p-5 bg-surface-1 border border-border rounded-xl hover:bg-surface-2 transition-all text-left"
              >
                <div className="w-10 h-10 rounded-xl bg-surface-2 border border-border flex items-center justify-center shrink-0 text-xl">
                  {a.avatar_emoji || <Bot className="w-5 h-5 text-text-muted" />}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-text-primary mb-0.5">{a.name}</p>
                  {a.description && (
                    <p className="text-xs text-text-muted truncate">{a.description}</p>
                  )}
                  <p className="text-[11px] text-text-muted mt-1">
                    {a.skill_ids.length} {a.skill_ids.length === 1 ? 'skill' : 'skills'} · {a.model_preference}
                  </p>
                </div>
              </button>
            ))}
          </div>
        )}
      </div>

      {showModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm">
          <div className="w-full max-w-md bg-surface-1 border border-border rounded-2xl shadow-xl p-6">
            <div className="flex items-center justify-between mb-5">
              <h2 className="text-sm font-semibold text-text-primary">Novo agente</h2>
              <button
                type="button"
                onClick={() => { setShowModal(false); setNewName(''); setNewDesc(''); setNewEmoji('') }}
                className="p-1 rounded-lg text-text-muted hover:text-text-primary hover:bg-surface-3 transition-colors"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="space-y-4">
              <div className="flex gap-3">
                <div>
                  <label className="block text-xs font-medium text-text-secondary mb-1.5">Emoji</label>
                  <input
                    type="text"
                    value={newEmoji}
                    onChange={(e) => setNewEmoji(e.target.value.slice(0, 2))}
                    placeholder="🤖"
                    className="w-16 bg-surface-2 border border-border rounded-xl px-3 py-2.5 text-sm text-center outline-none focus:border-accent/50 transition-colors"
                  />
                </div>
                <div className="flex-1">
                  <label className="block text-xs font-medium text-text-secondary mb-1.5">Nome</label>
                  <input
                    type="text"
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && handleCreate()}
                    placeholder="Ex: Pesquisador de mercado"
                    autoFocus
                    className="w-full bg-surface-2 border border-border rounded-xl px-4 py-2.5 text-sm text-text-primary placeholder:text-text-muted outline-none focus:border-accent/50 transition-colors"
                  />
                </div>
              </div>
              <div>
                <label className="block text-xs font-medium text-text-secondary mb-1.5">Descrição <span className="text-text-muted font-normal">(opcional)</span></label>
                <input
                  type="text"
                  value={newDesc}
                  onChange={(e) => setNewDesc(e.target.value)}
                  placeholder="Para que serve este agente?"
                  className="w-full bg-surface-2 border border-border rounded-xl px-4 py-2.5 text-sm text-text-primary placeholder:text-text-muted outline-none focus:border-accent/50 transition-colors"
                />
              </div>
            </div>

            <div className="flex gap-3 mt-6">
              <button
                type="button"
                onClick={() => { setShowModal(false); setNewName(''); setNewDesc(''); setNewEmoji('') }}
                className="flex-1 py-2.5 rounded-xl text-sm text-text-secondary bg-surface-2 hover:bg-surface-3 transition-colors"
              >
                Cancelar
              </button>
              <button
                type="button"
                onClick={handleCreate}
                disabled={!newName.trim() || creating}
                className="flex-1 py-2.5 rounded-xl text-sm font-medium text-text-inverse bg-accent hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {creating ? 'Criando…' : 'Criar agente'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
