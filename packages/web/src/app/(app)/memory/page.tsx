'use client'

import { useEffect, useState } from 'react'
import { Brain } from 'lucide-react'
import { apiListMemories, apiCreateMemory, apiDeleteMemory } from '@/api/client'
import type { Memory } from '@/types'
import { Card } from '@/components/ui/card'

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString('pt-BR', {
    day: '2-digit', month: 'short', year: 'numeric',
  })
}

export default function MemoryPage() {
  const [memories, setMemories] = useState<Memory[]>([])
  const [loading, setLoading]   = useState(true)
  const [error, setError]       = useState('')
  const [newContent, setNew]    = useState('')
  const [saving, setSaving]     = useState(false)
  const [deletingId, setDelId]  = useState<string | null>(null)

  useEffect(() => {
    apiListMemories()
      .then(setMemories)
      .catch(() => setError('Não foi possível carregar as memórias'))
      .finally(() => setLoading(false))
  }, [])

  async function handleAdd() {
    const content = newContent.trim()
    if (!content) return
    setSaving(true)
    setError('')
    try {
      const created = await apiCreateMemory(content)
      setMemories(prev => [created, ...prev])
      setNew('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Falha ao salvar memória')
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete(id: string) {
    setDelId(id)
    try {
      await apiDeleteMemory(id)
      setMemories(prev => prev.filter(m => m.id !== id))
    } catch {
      setError('Falha ao remover memória')
    } finally {
      setDelId(null)
    }
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-2xl mx-auto px-6 py-10">
        <div className="flex items-center gap-3 mb-8">
          <Brain className="w-5 h-5 text-accent-light" />
          <div>
            <h1 className="text-xl font-semibold text-text-primary">Memórias</h1>
            <p className="text-xs text-text-muted mt-0.5">
              Fatos que o TRINDATRON lembrará em todas as conversas futuras.
            </p>
          </div>
        </div>

        {/* Add memory */}
        <Card className="mb-6">
          <Card.Header>
            <span className="text-xs font-semibold text-text-muted uppercase tracking-wider">Nova memória</span>
          </Card.Header>
          <Card.Body>
            <div className="flex gap-2">
              <input
                type="text"
                value={newContent}
                onChange={e => setNew(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && !saving && handleAdd()}
                placeholder="Ex: Prefiro respostas em português"
                maxLength={2000}
                disabled={saving}
                className="flex-1 bg-surface-2 border border-border rounded-lg px-3 py-2 text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent/60 disabled:opacity-50"
              />
              <button
                onClick={handleAdd}
                disabled={saving || !newContent.trim()}
                className="px-4 py-2 text-xs font-medium bg-accent text-text-inverse rounded-lg hover:bg-accent/90 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
              >
                {saving ? 'Salvando…' : 'Salvar'}
              </button>
            </div>
            {error && <p className="text-xs text-error mt-2">{error}</p>}
          </Card.Body>
        </Card>

        {/* Memory list */}
        <Card>
          <Card.Header>
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-text-muted uppercase tracking-wider">
                Memórias salvas
              </span>
              {memories.length > 0 && (
                <span className="text-xs text-text-muted">{memories.length}</span>
              )}
            </div>
          </Card.Header>
          <Card.Body>
            {loading ? (
              <p className="text-sm text-text-muted">Carregando…</p>
            ) : memories.length === 0 ? (
              <p className="text-sm text-text-muted italic">Nenhuma memória salva ainda.</p>
            ) : (
              <ul className="space-y-2">
                {memories.map(m => (
                  <li
                    key={m.id}
                    className="flex items-start justify-between gap-3 bg-surface-2 rounded-lg px-3 py-3"
                  >
                    <div className="flex-1 min-w-0">
                      <p className="text-sm text-text-primary leading-snug">{m.content}</p>
                      <p className="text-[10px] text-text-muted mt-1">{formatDate(m.created_at)}</p>
                    </div>
                    <button
                      onClick={() => handleDelete(m.id)}
                      disabled={deletingId === m.id}
                      aria-label="Remover memória"
                      className="shrink-0 text-text-muted hover:text-error transition-colors disabled:opacity-40 mt-0.5 text-base leading-none"
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
