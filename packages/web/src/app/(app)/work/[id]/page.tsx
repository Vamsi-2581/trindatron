'use client'

import { useEffect, useRef, useState } from 'react'
import { useParams } from 'next/navigation'
import Link from 'next/link'
import { AlertTriangle, ArrowLeft, CheckCircle, Briefcase, Lock, X, Zap } from 'lucide-react'
import {
  apiGetWorkWorkspace,
  apiRunWorkTask,
  apiApproveWorkAction,
  apiRejectWorkAction,
  type WorkApprovalEvent,
} from '@/api/client'
import type { WorkWorkspace } from '@/types'
import { useAppStore } from '@/store/useAppStore'

type ActivityEvent = {
  id: number
  key: string
  message: string
}

type TaskResult = {
  answer: string
  iterations: number
  duration_ms: number
}

export default function WorkWorkspacePage() {
  const { id } = useParams<{ id: string }>()
  const { entitlement } = useAppStore()

  // All hooks must be declared before any conditional return (React Rules of Hooks).
  const [workspace, setWorkspace] = useState<WorkWorkspace | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Task state
  const [task, setTask] = useState('')
  const [taskRunning, setTaskRunning] = useState(false)
  const [taskResult, setTaskResult] = useState<TaskResult | null>(null)
  const [taskError, setTaskError] = useState('')

  // Activity stream
  const [activityStream, setActivityStream] = useState<ActivityEvent[]>([])
  const activityRef = useRef<HTMLDivElement>(null)
  const eventCounter = useRef(0)

  // Approval gate
  const [pendingApproval, setPendingApproval] = useState<WorkApprovalEvent | null>(null)
  const [approvalLoading, setApprovalLoading] = useState(false)

  useEffect(() => {
    apiGetWorkWorkspace(id)
      .then(setWorkspace)
      .catch(() => setError('Workspace não encontrado'))
      .finally(() => setLoading(false))
  }, [id])

  useEffect(() => {
    if (activityRef.current) {
      activityRef.current.scrollTop = activityRef.current.scrollHeight
    }
  }, [activityStream])

  if (entitlement && !entitlement.features.supports_trinity_work) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-4 text-center px-6">
        <div className="w-12 h-12 rounded-full bg-surface-3 flex items-center justify-center">
          <Lock className="w-5 h-5 text-text-muted" />
        </div>
        <p className="text-sm font-medium text-text-primary">TRINITY WORK requer o plano PRO ou superior</p>
        <Link href="/work" className="text-xs text-accent-light hover:text-accent transition-colors">← Voltar</Link>
      </div>
    )
  }

  async function handleRunTask() {
    if (!workspace || taskRunning || !task.trim()) return
    setTaskRunning(true)
    setActivityStream([])
    setTaskResult(null)
    setTaskError('')
    setPendingApproval(null)

    await apiRunWorkTask(
      workspace.id,
      task.trim(),
      8,
      (key, message) => {
        setActivityStream((prev) => [...prev, { id: ++eventCounter.current, key, message }])
      },
      (event) => {
        setPendingApproval(event)
        setActivityStream((prev) => [...prev, {
          id: ++eventCounter.current,
          key: 'activity.work.approval_required',
          message: 'Aguardando aprovação',
        }])
      },
      (result) => {
        setTaskResult(result)
        setTaskRunning(false)
      },
      (msg) => {
        setTaskError(msg)
        setTaskRunning(false)
      },
    )
  }

  function handleTaskKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      void handleRunTask()
    }
  }

  async function handleApprove() {
    if (!workspace || !pendingApproval || approvalLoading) return
    setApprovalLoading(true)
    try {
      await apiApproveWorkAction(workspace.id, pendingApproval.action_id)
      setPendingApproval(null)
      setActivityStream((prev) => [...prev, {
        id: ++eventCounter.current,
        key: 'activity.work.approved',
        message: 'Ação aprovada',
      }])
    } catch (err) {
      setTaskError(err instanceof Error ? err.message : 'Falha ao aprovar ação')
    } finally {
      setApprovalLoading(false)
    }
  }

  async function handleReject() {
    if (!workspace || !pendingApproval || approvalLoading) return
    setApprovalLoading(true)
    try {
      await apiRejectWorkAction(workspace.id, pendingApproval.action_id)
      setPendingApproval(null)
      setTaskRunning(false)
      setActivityStream((prev) => [...prev, {
        id: ++eventCounter.current,
        key: 'activity.work.rejected',
        message: 'Ação rejeitada',
      }])
    } catch (err) {
      setTaskError(err instanceof Error ? err.message : 'Falha ao rejeitar ação')
    } finally {
      setApprovalLoading(false)
    }
  }

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="w-5 h-5 border-2 border-accent border-t-transparent rounded-full animate-spin" />
      </div>
    )
  }

  if (error || !workspace) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-3 text-center px-6">
        <p className="text-sm text-text-muted">{error || 'Workspace não encontrado'}</p>
        <Link href="/projects" className="text-xs text-accent-light hover:text-accent transition-colors">
          ← Voltar para projetos
        </Link>
      </div>
    )
  }

  return (
    <div className="flex-1 flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-3 px-6 py-4 border-b border-border shrink-0">
        <Link
          href={`/projects/${workspace.project_id}`}
          className="p-1.5 rounded-lg text-text-muted hover:text-text-primary hover:bg-surface-2 transition-colors"
        >
          <ArrowLeft className="w-4 h-4" />
        </Link>
        <Briefcase className="w-4 h-4 text-text-muted" />
        <h1 className="text-sm font-semibold text-text-primary">TRINITY WORK</h1>
        {workspace.approval_policy && (
          <span className="text-xs text-text-muted bg-surface-2 px-2 py-0.5 rounded-md capitalize">
            {workspace.approval_policy}
          </span>
        )}
      </div>

      {/* Main content */}
      <div className="flex-1 flex flex-col overflow-hidden min-h-0 px-6 py-4 gap-4">

        {/* Activity Stream */}
        {activityStream.length > 0 && (
          <div className="shrink-0 border border-border rounded-xl overflow-hidden">
            <div className="flex items-center gap-2 px-4 py-2 bg-surface-1 border-b border-border">
              <Zap className="w-3 h-3 text-accent-light" />
              <span className="text-[11px] font-medium text-text-muted">Activity Stream</span>
              {taskRunning && !pendingApproval && (
                <div className="w-2 h-2 bg-accent rounded-full animate-pulse ml-auto" />
              )}
            </div>
            <div
              ref={activityRef}
              className="max-h-36 overflow-y-auto px-4 py-2 space-y-1.5 bg-surface-1"
            >
              {activityStream.map((ev) => (
                <div key={ev.id} className="flex items-start gap-2 text-[11px]">
                  <span className="shrink-0 text-[10px] font-mono text-text-muted bg-surface-2 px-1.5 py-0.5 rounded leading-none mt-0.5">
                    {ev.key.split('.').slice(-1)[0] ?? ev.key}
                  </span>
                  <span className="text-text-secondary leading-relaxed">{ev.message}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Approval gate panel */}
        {pendingApproval && (
          <div className="shrink-0 border border-yellow-500/40 rounded-xl overflow-hidden">
            <div className="flex items-center gap-2 px-4 py-2 bg-yellow-500/10 border-b border-yellow-500/20">
              <AlertTriangle className="w-3.5 h-3.5 text-yellow-400" />
              <span className="text-xs font-medium text-yellow-300">Aprovação necessária</span>
            </div>
            <div className="px-4 py-3 bg-surface-1 space-y-3">
              <p className="text-sm text-text-secondary">{pendingApproval.description}</p>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => void handleApprove()}
                  disabled={approvalLoading}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-green-600 hover:bg-green-500 text-white text-xs font-medium transition-colors disabled:opacity-50"
                >
                  <CheckCircle className="w-3.5 h-3.5" />
                  Aprovar
                </button>
                <button
                  type="button"
                  onClick={() => void handleReject()}
                  disabled={approvalLoading}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-surface-2 hover:bg-surface-3 text-text-secondary text-xs font-medium transition-colors disabled:opacity-50"
                >
                  <X className="w-3.5 h-3.5" />
                  Rejeitar
                </button>
              </div>
            </div>
          </div>
        )}

        {/* Task result */}
        {taskResult && !taskRunning && (
          <div className="flex-1 min-h-0 border border-border rounded-xl overflow-hidden flex flex-col">
            <div className="flex items-center justify-between px-4 py-2 bg-surface-1 border-b border-border shrink-0">
              <span className="text-xs font-medium text-green-400">
                Concluído · {taskResult.iterations} iteração{taskResult.iterations !== 1 ? 'ões' : ''} · {taskResult.duration_ms}ms
              </span>
              <button
                type="button"
                onClick={() => setTaskResult(null)}
                className="text-text-muted hover:text-text-primary"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto p-4">
              <p className="text-sm text-text-primary leading-relaxed whitespace-pre-wrap">{taskResult.answer}</p>
            </div>
          </div>
        )}

        {/* Error */}
        {taskError && !taskRunning && (
          <div className="shrink-0 border border-red-500/30 rounded-xl px-4 py-3 bg-surface-1 flex items-center gap-2">
            <p className="flex-1 text-sm text-red-400">{taskError}</p>
            <button type="button" onClick={() => setTaskError('')} className="text-text-muted hover:text-text-primary">
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        )}

        {/* Empty state */}
        {!taskRunning && !taskResult && !taskError && activityStream.length === 0 && (
          <div className="flex-1 flex flex-col items-center justify-center text-center gap-2">
            <Briefcase className="w-8 h-8 text-text-muted/40" />
            <p className="text-sm text-text-muted">Descreva uma tarefa e o TRINDATRON irá pesquisar, criar documentos e coordenar ações.</p>
          </div>
        )}
      </div>

      {/* Task input */}
      <div className="shrink-0 border-t border-border px-4 py-3 bg-surface-1">
        <div className="flex items-end gap-3">
          <textarea
            value={task}
            onChange={(e) => setTask(e.target.value)}
            onKeyDown={handleTaskKeyDown}
            disabled={taskRunning}
            placeholder="Descreva o que precisa ser feito… (Ctrl+Enter para executar)"
            rows={2}
            className="flex-1 bg-surface-2 border border-border rounded-lg px-3 py-2 text-sm text-text-primary placeholder:text-text-muted outline-none resize-none focus:border-accent transition-colors disabled:opacity-50"
          />
          <button
            type="button"
            onClick={() => void handleRunTask()}
            disabled={!task.trim() || taskRunning}
            className="shrink-0 flex items-center gap-1.5 px-3 py-2 rounded-lg bg-accent text-text-inverse text-xs font-medium hover:bg-accent/80 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {taskRunning
              ? <><div className="w-3 h-3 border border-white border-t-transparent rounded-full animate-spin" /><span>Executando</span></>
              : <><Zap className="w-3.5 h-3.5" /><span>Executar</span></>
            }
          </button>
        </div>
        <p className="text-[10px] text-text-muted mt-1.5">
          Ações com efeito externo (notificações, agendamentos) requerem sua aprovação antes de serem executadas.
        </p>
      </div>
    </div>
  )
}
