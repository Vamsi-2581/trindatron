'use client'

import { useEffect, useState, useRef } from 'react'
import { useParams, useRouter } from 'next/navigation'
import Link from 'next/link'
import {
  ArrowLeft, FileText, Trash2, Upload, MessageSquare,
  Settings, AlertCircle, Plus, Code2,
} from 'lucide-react'
import { useAppStore } from '@/store/useAppStore'
import {
  apiGetProject,
  apiUpdateProject,
  apiArchiveProject,
  apiUploadProjectFile,
  apiDeleteProjectFile,
  apiGetProjectSessions,
  apiCreateSession,
  apiCreateCodeWorkspace,
  apiGetProjectCodeWorkspace,
} from '@/api/client'
import type { Project, Session, CodeWorkspace } from '@/types'

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString('pt-BR', { day: '2-digit', month: 'short', year: 'numeric' })
}

export default function ProjectDetailPage() {
  const { id } = useParams<{ id: string }>()
  const router = useRouter()
  const { updateProject: updateProjectStore, removeProject, prependSession, setPendingMessage } = useAppStore()

  const [project, setProject] = useState<Project | null>(null)
  const [sessions, setSessions] = useState<Session[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [editingInstructions, setEditingInstructions] = useState(false)
  const [instructionsValue, setInstructionsValue] = useState('')
  const [savingInstructions, setSavingInstructions] = useState(false)

  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState('')
  const fileInputRef = useRef<HTMLInputElement>(null)

  const [creatingSession, setCreatingSession] = useState(false)

  const [codeWorkspace, setCodeWorkspace] = useState<CodeWorkspace | null | undefined>(undefined)
  const [creatingCodeWs, setCreatingCodeWs] = useState(false)

  useEffect(() => {
    Promise.all([apiGetProject(id), apiGetProjectSessions(id), apiGetProjectCodeWorkspace(id)])
      .then(([proj, sess, ws]) => {
        setProject(proj)
        setInstructionsValue(proj.instructions ?? '')
        setSessions(sess.sessions)
        setCodeWorkspace(ws)
      })
      .catch(() => setError('Projeto não encontrado'))
      .finally(() => setLoading(false))
  }, [id])

  async function handleSaveInstructions() {
    if (!project || savingInstructions) return
    setSavingInstructions(true)
    try {
      const updated = await apiUpdateProject(id, { instructions: instructionsValue || null })
      setProject(updated)
      updateProjectStore(updated)
      setEditingInstructions(false)
    } catch {
      setError('Não foi possível salvar as instruções')
    } finally {
      setSavingInstructions(false)
    }
  }

  async function handleUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file || !project) return
    setUploading(true)
    setUploadError('')
    try {
      const pf = await apiUploadProjectFile(project.id, file)
      setProject((p) => p ? { ...p, files: [...p.files, pf] } : p)
    } catch (err: unknown) {
      setUploadError(err instanceof Error ? err.message : 'Falha ao enviar arquivo')
    } finally {
      setUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  async function handleDeleteFile(fileId: string) {
    if (!project) return
    try {
      await apiDeleteProjectFile(project.id, fileId)
      setProject((p) => p ? { ...p, files: p.files.filter((f) => f.id !== fileId) } : p)
    } catch {
      setError('Não foi possível remover o arquivo')
    }
  }

  async function handleNewConversation() {
    if (!project || creatingSession) return
    setCreatingSession(true)
    try {
      const session = await apiCreateSession(project.name, project.id)
      prependSession(session)
      setPendingMessage('')
      router.push(`/chat/${session.id}`)
    } catch {
      setError('Não foi possível criar a conversa')
      setCreatingSession(false)
    }
  }

  async function handleCreateCodeWorkspace() {
    if (!project || creatingCodeWs) return
    setCreatingCodeWs(true)
    try {
      const ws = await apiCreateCodeWorkspace(project.id)
      setCodeWorkspace(ws)
    } catch {
      setError('Não foi possível criar o CODE workspace')
    } finally {
      setCreatingCodeWs(false)
    }
  }

  async function handleArchive() {
    if (!project) return
    if (!confirm(`Arquivar o projeto "${project.name}"? Ele não aparecerá mais na lista.`)) return
    try {
      await apiArchiveProject(project.id)
      removeProject(project.id)
      router.push('/projects')
    } catch {
      setError('Não foi possível arquivar o projeto')
    }
  }

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="w-5 h-5 border-2 border-accent border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  if (error && !project) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-3 text-center px-6">
        <AlertCircle className="w-8 h-8 text-text-muted" />
        <p className="text-sm text-text-muted">{error}</p>
        <Link href="/projects" className="text-xs text-accent-light hover:text-accent transition-colors">
          ← Voltar para projetos
        </Link>
      </div>
    )
  }

  if (!project) return null

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-2xl mx-auto px-6 py-10">

        {/* Header */}
        <div className="flex items-start gap-4 mb-8">
          <Link href="/projects" className="mt-0.5 p-1.5 rounded-lg text-text-muted hover:text-text-primary hover:bg-surface-2 transition-colors">
            <ArrowLeft className="w-4 h-4" />
          </Link>
          <div className="flex-1 min-w-0">
            <h1 className="text-xl font-semibold text-text-primary truncate">{project.name}</h1>
            {project.description && (
              <p className="text-sm text-text-muted mt-0.5">{project.description}</p>
            )}
          </div>
          <button
            type="button"
            onClick={handleNewConversation}
            disabled={creatingSession}
            className="flex items-center gap-2 px-4 py-2 bg-accent hover:bg-accent-hover text-text-inverse text-sm font-medium rounded-xl transition-colors disabled:opacity-50 disabled:cursor-not-allowed shrink-0"
          >
            <Plus className="w-4 h-4" />
            {creatingSession ? 'Criando…' : 'Nova conversa'}
          </button>
        </div>

        {error && (
          <p className="text-sm text-error mb-5">{error}</p>
        )}

        {/* Instructions */}
        <div className="bg-surface-1 border border-border rounded-2xl p-5 mb-5">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <Settings className="w-3.5 h-3.5 text-text-muted" />
              <span className="text-xs font-semibold text-text-muted uppercase tracking-wider">Instruções do projeto</span>
            </div>
            {!editingInstructions && (
              <button
                type="button"
                onClick={() => setEditingInstructions(true)}
                className="text-xs text-accent-light hover:text-accent transition-colors"
              >
                Editar
              </button>
            )}
          </div>

          {editingInstructions ? (
            <div>
              <textarea
                value={instructionsValue}
                onChange={(e) => setInstructionsValue(e.target.value)}
                rows={5}
                placeholder="Ex: Você é um assistente especializado em finanças. Sempre use linguagem formal e forneça dados com fontes."
                className="w-full bg-surface-2 border border-border rounded-xl px-4 py-3 text-sm text-text-primary placeholder:text-text-muted resize-none outline-none focus:border-accent/50 transition-colors leading-relaxed"
              />
              <div className="flex gap-2 mt-3">
                <button
                  type="button"
                  onClick={() => { setEditingInstructions(false); setInstructionsValue(project.instructions ?? '') }}
                  className="px-4 py-1.5 rounded-lg text-xs text-text-secondary bg-surface-2 hover:bg-surface-3 transition-colors"
                >
                  Cancelar
                </button>
                <button
                  type="button"
                  onClick={handleSaveInstructions}
                  disabled={savingInstructions}
                  className="px-4 py-1.5 rounded-lg text-xs font-medium text-text-inverse bg-accent hover:bg-accent-hover disabled:opacity-50 transition-colors"
                >
                  {savingInstructions ? 'Salvando…' : 'Salvar'}
                </button>
              </div>
            </div>
          ) : (
            <p className="text-sm text-text-secondary leading-relaxed">
              {project.instructions || (
                <span className="text-text-muted italic">
                  Nenhuma instrução definida. As instruções são injetadas no contexto de todas as conversas deste projeto.
                </span>
              )}
            </p>
          )}
        </div>

        {/* Files */}
        <div className="bg-surface-1 border border-border rounded-2xl p-5 mb-5">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <FileText className="w-3.5 h-3.5 text-text-muted" />
              <span className="text-xs font-semibold text-text-muted uppercase tracking-wider">Arquivos</span>
              <span className="text-xs text-text-muted">({project.files.length})</span>
            </div>
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading}
              className="flex items-center gap-1.5 text-xs text-accent-light hover:text-accent disabled:opacity-50 transition-colors"
            >
              <Upload className="w-3.5 h-3.5" />
              {uploading ? 'Enviando…' : 'Adicionar arquivo'}
            </button>
            <input
              ref={fileInputRef}
              type="file"
              onChange={handleUpload}
              className="hidden"
            />
          </div>

          {uploadError && (
            <p className="text-xs text-error mb-3">{uploadError}</p>
          )}

          {project.files.length === 0 ? (
            <p className="text-xs text-text-muted text-center py-4">
              Nenhum arquivo. Os arquivos adicionados ficam disponíveis como contexto nas conversas.
            </p>
          ) : (
            <div className="space-y-2">
              {project.files.map((f) => (
                <div key={f.id} className="flex items-center gap-3 p-3 bg-surface-2 rounded-xl">
                  <FileText className="w-4 h-4 text-text-muted shrink-0" />
                  <div className="flex-1 min-w-0">
                    <p className="text-xs text-text-primary truncate">{f.name}</p>
                    <p className="text-[11px] text-text-muted">{formatBytes(f.size_bytes)} · {formatDate(f.created_at)}</p>
                  </div>
                  <button
                    type="button"
                    onClick={() => handleDeleteFile(f.id)}
                    className="p-1 rounded-lg text-text-muted hover:text-error hover:bg-surface-3 transition-colors"
                    title="Remover arquivo"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* CODE Workspace */}
        {codeWorkspace !== undefined && (
          <div className="bg-surface-1 border border-border rounded-2xl p-5 mb-5">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <Code2 className="w-3.5 h-3.5 text-text-muted" />
                <span className="text-xs font-semibold text-text-muted uppercase tracking-wider">TRINITY CODE</span>
              </div>
              {codeWorkspace && (
                <Link
                  href={`/code/${codeWorkspace.id}`}
                  className="text-xs text-accent-light hover:text-accent transition-colors"
                >
                  Abrir workspace →
                </Link>
              )}
            </div>
            {codeWorkspace ? (
              <p className="text-xs text-text-secondary leading-relaxed">
                Workspace criado em {formatDate(codeWorkspace.created_at)}.
                {codeWorkspace.language_hint && ` Linguagem: ${codeWorkspace.language_hint}.`}
              </p>
            ) : (
              <div className="flex items-center justify-between">
                <p className="text-xs text-text-muted">Nenhum CODE workspace. Crie um para executar comandos neste projeto.</p>
                <button
                  type="button"
                  onClick={handleCreateCodeWorkspace}
                  disabled={creatingCodeWs}
                  className="flex items-center gap-1.5 text-xs text-accent-light hover:text-accent disabled:opacity-50 transition-colors shrink-0 ml-4"
                >
                  <Plus className="w-3.5 h-3.5" />
                  {creatingCodeWs ? 'Criando…' : 'Criar'}
                </button>
              </div>
            )}
          </div>
        )}

        {/* Recent Sessions */}
        <div className="bg-surface-1 border border-border rounded-2xl p-5 mb-5">
          <div className="flex items-center gap-2 mb-4">
            <MessageSquare className="w-3.5 h-3.5 text-text-muted" />
            <span className="text-xs font-semibold text-text-muted uppercase tracking-wider">Conversas</span>
            <span className="text-xs text-text-muted">({sessions.length})</span>
          </div>

          {sessions.length === 0 ? (
            <p className="text-xs text-text-muted text-center py-4">
              Nenhuma conversa ainda. Clique em "Nova conversa" para começar.
            </p>
          ) : (
            <div className="space-y-2">
              {sessions.slice(0, 10).map((s) => (
                <Link
                  key={s.id}
                  href={`/chat/${s.id}`}
                  className="flex items-center gap-3 p-3 bg-surface-2 rounded-xl hover:bg-surface-3 transition-colors"
                >
                  <MessageSquare className="w-3.5 h-3.5 text-text-muted shrink-0" />
                  <span className="text-xs text-text-secondary truncate flex-1">{s.title}</span>
                  <span className="text-[11px] text-text-muted shrink-0">{formatDate(s.updated_at)}</span>
                </Link>
              ))}
            </div>
          )}
        </div>

        {/* Danger zone */}
        <div className="border border-error/20 rounded-2xl p-5">
          <p className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-3">Zona de risco</p>
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-text-primary">Arquivar projeto</p>
              <p className="text-xs text-text-muted mt-0.5">O projeto não aparecerá mais na lista. As conversas são preservadas.</p>
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
