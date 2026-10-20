'use client'

import { useEffect, useState } from 'react'
import { useParams, useRouter } from 'next/navigation'
import Link from 'next/link'
import { ArrowLeft, Bot, Trash2, AlertCircle, Check } from 'lucide-react'
import { useAppStore } from '@/store/useAppStore'
import {
  apiGetAgent, apiUpdateAgent, apiArchiveAgent, apiListSkills,
} from '@/api/client'
import type { AgentDefinition, Skill } from '@/types'

const PREFERENCE_LABELS: Record<string, string> = {
  power: 'Máximo desempenho',
  balanced: 'Balanceado',
  fast: 'Rápido',
}

export default function AgentDetailPage() {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()
  const { updateAgentDefinition, removeAgentDefinition } = useAppStore()

  const [agent, setAgent] = useState<AgentDefinition | null>(null)
  const [skills, setSkills] = useState<Skill[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  const [name, setName] = useState('')
  const [emoji, setEmoji] = useState('')
  const [description, setDescription] = useState('')
  const [instructions, setInstructions] = useState('')
  const [selectedSkills, setSelectedSkills] = useState<string[]>([])
  const [preference, setPreference] = useState<'power' | 'balanced' | 'fast'>('balanced')

  useEffect(() => {
    Promise.all([apiGetAgent(id), apiListSkills()])
      .then(([a, s]) => {
        setAgent(a)
        setSkills(s.skills)
        setName(a.name)
        setEmoji(a.avatar_emoji ?? '')
        setDescription(a.description ?? '')
        setInstructions(a.system_instructions ?? '')
        setSelectedSkills(a.skill_ids)
        setPreference(a.model_preference)
      })
      .catch(() => setError('Agente não encontrado'))
      .finally(() => setLoading(false))
  }, [id])

  function toggleSkill(skillId: string) {
    setSelectedSkills((prev) =>
      prev.includes(skillId) ? prev.filter((s) => s !== skillId) : [...prev, skillId],
    )
  }

  async function handleSave() {
    if (!agent || saving) return
    setSaving(true)
    try {
      const updated = await apiUpdateAgent(id, {
        name: name.trim() || agent.name,
        description: description.trim() || null,
        avatar_emoji: emoji.trim() || null,
        system_instructions: instructions.trim() || null,
        skill_ids: selectedSkills,
        model_preference: preference,
      })
      setAgent(updated)
      updateAgentDefinition(updated)
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } catch {
      setError('Não foi possível salvar o agente')
    } finally {
      setSaving(false)
    }
  }

  async function handleArchive() {
    if (!agent) return
    if (!confirm(`Arquivar o agente "${agent.name}"? Ele não aparecerá mais na lista.`)) return
    try {
      await apiArchiveAgent(agent.id)
      removeAgentDefinition(agent.id)
      router.push('/agents')
    } catch {
      setError('Não foi possível arquivar o agente')
    }
  }

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="w-5 h-5 border-2 border-accent border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  if (error && !agent) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-3 text-center px-6">
        <AlertCircle className="w-8 h-8 text-text-muted" />
        <p className="text-sm text-text-muted">{error}</p>
        <Link href="/agents" className="text-xs text-accent-light hover:text-accent transition-colors">
          ← Voltar para agentes
        </Link>
      </div>
    )
  }

  if (!agent) return null

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-2xl mx-auto px-6 py-10">

        {/* Header */}
        <div className="flex items-start gap-4 mb-8">
          <Link href="/agents" className="mt-0.5 p-1.5 rounded-lg text-text-muted hover:text-text-primary hover:bg-surface-2 transition-colors">
            <ArrowLeft className="w-4 h-4" />
          </Link>
          <div className="flex-1 min-w-0">
            <h1 className="text-xl font-semibold text-text-primary truncate">{agent.name}</h1>
            {agent.description && <p className="text-sm text-text-muted mt-0.5">{agent.description}</p>}
          </div>
          <button
            type="button"
            onClick={handleSave}
            disabled={saving}
            className="flex items-center gap-2 px-4 py-2 bg-accent hover:bg-accent-hover text-text-inverse text-sm font-medium rounded-xl transition-colors disabled:opacity-50 shrink-0"
          >
            {saved ? <><Check className="w-4 h-4" /> Salvo</> : saving ? 'Salvando…' : 'Salvar'}
          </button>
        </div>

        {error && <p className="text-sm text-error mb-5">{error}</p>}

        {/* Identity */}
        <div className="bg-surface-1 border border-border rounded-2xl p-5 mb-5">
          <p className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-4">Identidade</p>
          <div className="flex gap-3 mb-4">
            <div>
              <label className="block text-xs font-medium text-text-secondary mb-1.5">Emoji</label>
              <input
                type="text"
                value={emoji}
                onChange={(e) => setEmoji(e.target.value.slice(0, 2))}
                placeholder="🤖"
                className="w-16 bg-surface-2 border border-border rounded-xl px-3 py-2 text-sm text-center outline-none focus:border-accent/50 transition-colors"
              />
            </div>
            <div className="flex-1">
              <label className="block text-xs font-medium text-text-secondary mb-1.5">Nome</label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="w-full bg-surface-2 border border-border rounded-xl px-4 py-2 text-sm text-text-primary outline-none focus:border-accent/50 transition-colors"
              />
            </div>
          </div>
          <div>
            <label className="block text-xs font-medium text-text-secondary mb-1.5">Descrição</label>
            <input
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Para que serve este agente?"
              className="w-full bg-surface-2 border border-border rounded-xl px-4 py-2 text-sm text-text-primary placeholder:text-text-muted outline-none focus:border-accent/50 transition-colors"
            />
          </div>
        </div>

        {/* Instructions */}
        <div className="bg-surface-1 border border-border rounded-2xl p-5 mb-5">
          <p className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-3">Instruções do agente</p>
          <textarea
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            rows={5}
            placeholder="Ex: Você é um especialista em análise de mercado financeiro. Use linguagem técnica e forneça dados com fontes."
            className="w-full bg-surface-2 border border-border rounded-xl px-4 py-3 text-sm text-text-primary placeholder:text-text-muted resize-none outline-none focus:border-accent/50 transition-colors leading-relaxed"
          />
          <p className="text-[11px] text-text-muted mt-2">Injetadas no início de cada conversa que utilizar este agente.</p>
        </div>

        {/* Skills */}
        <div className="bg-surface-1 border border-border rounded-2xl p-5 mb-5">
          <p className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-4">Skills</p>
          <div className="space-y-2">
            {skills.map((skill) => {
              const active = selectedSkills.includes(skill.id)
              return (
                <button
                  key={skill.id}
                  type="button"
                  onClick={() => toggleSkill(skill.id)}
                  className={`w-full flex items-start gap-3 p-3 rounded-xl border transition-all text-left ${
                    active
                      ? 'bg-accent/10 border-accent/30'
                      : 'bg-surface-2 border-border hover:border-border'
                  }`}
                >
                  <span className="text-lg shrink-0">{skill.icon_emoji ?? '🔧'}</span>
                  <div className="flex-1 min-w-0">
                    <p className={`text-xs font-medium ${active ? 'text-accent-light' : 'text-text-secondary'}`}>
                      {skill.name}
                      {skill.is_builtin && (
                        <span className="ml-1.5 text-[10px] text-text-muted font-normal">nativo</span>
                      )}
                    </p>
                    {skill.description && (
                      <p className="text-[11px] text-text-muted mt-0.5">{skill.description}</p>
                    )}
                    {skill.tool_ids.length > 0 && (
                      <p className="text-[10px] text-text-muted mt-1">
                        Ferramentas: {skill.tool_ids.join(', ')}
                      </p>
                    )}
                  </div>
                  {active && <Check className="w-3.5 h-3.5 text-accent-light mt-0.5 shrink-0" />}
                </button>
              )
            })}
          </div>
        </div>

        {/* Model preference */}
        <div className="bg-surface-1 border border-border rounded-2xl p-5 mb-5">
          <p className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-3">Preferência de modelo</p>
          <div className="flex gap-2">
            {(['power', 'balanced', 'fast'] as const).map((p) => (
              <button
                key={p}
                type="button"
                onClick={() => setPreference(p)}
                className={`flex-1 py-2 rounded-xl text-xs font-medium border transition-all ${
                  preference === p
                    ? 'bg-accent/10 border-accent/30 text-accent-light'
                    : 'bg-surface-2 border-border text-text-muted hover:text-text-secondary'
                }`}
              >
                {PREFERENCE_LABELS[p]}
              </button>
            ))}
          </div>
          <p className="text-[11px] text-text-muted mt-2">A seleção real do modelo é feita automaticamente pelo sistema.</p>
        </div>

        {/* Danger zone */}
        <div className="border border-error/20 rounded-2xl p-5">
          <p className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-3">Zona de risco</p>
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-text-primary">Arquivar agente</p>
              <p className="text-xs text-text-muted mt-0.5">O agente não aparecerá mais na lista.</p>
            </div>
            <button
              type="button"
              onClick={handleArchive}
              className="flex items-center gap-2 px-4 py-2 text-sm text-error border border-error/30 rounded-xl hover:bg-error/5 transition-colors"
            >
              <Trash2 className="w-4 h-4" />
              Arquivar
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
