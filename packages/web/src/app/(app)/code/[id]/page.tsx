'use client'

import { useEffect, useRef, useState } from 'react'
import { useParams } from 'next/navigation'
import Link from 'next/link'
import {
  ArrowLeft, ChevronDown, ChevronRight, File, Folder,
  Play, Square, Terminal, X, Zap,
} from 'lucide-react'
import {
  apiGetCodeWorkspace,
  apiListWorkspaceFiles,
  apiReadWorkspaceFile,
  apiExecuteCode,
  apiRunCodeTask,
} from '@/api/client'
import type { CodeWorkspace, WorkspaceFileEntry } from '@/types'
import { useAppStore } from '@/store/useAppStore'
import { Lock } from 'lucide-react'

type DirChildren = Record<string, WorkspaceFileEntry[]>

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

export default function CodeWorkspacePage() {
  const { id } = useParams<{ id: string }>()
  const { entitlement } = useAppStore()

  // All hooks must be declared before any conditional return (React Rules of Hooks).
  const [workspace, setWorkspace] = useState<CodeWorkspace | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Shell terminal
  const [command, setCommand] = useState('')
  const [output, setOutput] = useState<string[]>([])
  const [running, setRunning] = useState(false)
  const [lastResult, setLastResult] = useState<{ success: boolean; exit_code: number; duration_ms: number } | null>(null)
  const outputRef = useRef<HTMLDivElement>(null)

  // AI task
  const [task, setTask] = useState('')
  const [taskRunning, setTaskRunning] = useState(false)
  const [activityStream, setActivityStream] = useState<ActivityEvent[]>([])
  const [taskResult, setTaskResult] = useState<TaskResult | null>(null)
  const [taskError, setTaskError] = useState('')
  const activityRef = useRef<HTMLDivElement>(null)
  const eventCounter = useRef(0)

  // File tree
  const [treeRoot, setTreeRoot] = useState<WorkspaceFileEntry[]>([])
  const [dirChildren, setDirChildren] = useState<DirChildren>({})
  const [expandedDirs, setExpandedDirs] = useState<Set<string>>(new Set())
  const [treeError, setTreeError] = useState('')

  // File viewer
  const [viewingFile, setViewingFile] = useState<{ path: string; content: string } | null>(null)
  const [fileLoading, setFileLoading] = useState<string | null>(null)

  useEffect(() => {
    apiGetCodeWorkspace(id)
      .then((ws) => {
        setWorkspace(ws)
        if (ws.root_path) {
          apiListWorkspaceFiles(ws.id)
            .then(setTreeRoot)
            .catch(() => setTreeError('Não foi possível carregar arquivos'))
        }
      })
      .catch(() => setError('Workspace não encontrado'))
      .finally(() => setLoading(false))
  }, [id])

  useEffect(() => {
    if (outputRef.current) {
      outputRef.current.scrollTop = outputRef.current.scrollHeight
    }
  }, [output])

  useEffect(() => {
    if (activityRef.current) {
      activityRef.current.scrollTop = activityRef.current.scrollHeight
    }
  }, [activityStream])

  if (entitlement && !entitlement.features.supports_trinity_code) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-4 text-center px-6">
        <div className="w-12 h-12 rounded-full bg-surface-3 flex items-center justify-center">
          <Lock className="w-5 h-5 text-text-muted" />
        </div>
        <p className="text-sm font-medium text-text-primary">TRINITY CODE requer o plano START ou superior</p>
        <Link href="/code" className="text-xs text-accent-light hover:text-accent transition-colors">← Voltar</Link>
      </div>
    )
  }

  async function handleRun() {
    if (!workspace || running || !command.trim()) return
    setRunning(true)
    setOutput([])
    setLastResult(null)
    await apiExecuteCode(
      workspace.id,
      command.trim(),
      30,
      (data) => setOutput((prev) => [...prev, data]),
      (result) => { setLastResult(result); setRunning(false) },
      (msg) => { setOutput((prev) => [...prev, `Erro: ${msg}`]); setRunning(false) },
    )
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void handleRun() }
  }

  async function handleRunTask() {
    if (!workspace || taskRunning || !task.trim()) return
    setTaskRunning(true)
    setActivityStream([])
    setTaskResult(null)
    setTaskError('')

    await apiRunCodeTask(
      workspace.id,
      task.trim(),
      10,
      (key, message) => {
        const ev: ActivityEvent = { id: ++eventCounter.current, key, message }
        setActivityStream((prev) => [...prev, ev])
      },
      (result) => {
        setTaskResult(result)
        setTaskRunning(false)
        // Refresh file tree after task completes in case files were written
        if (workspace.root_path) {
          apiListWorkspaceFiles(workspace.id)
            .then(setTreeRoot)
            .catch(() => {/* ignore */ })
        }
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

  async function handleToggleDir(entry: WorkspaceFileEntry) {
    if (!workspace) return
    if (expandedDirs.has(entry.path)) {
      setExpandedDirs((prev) => { const s = new Set(prev); s.delete(entry.path); return s })
      return
    }
    try {
      const children = await apiListWorkspaceFiles(workspace.id, entry.path)
      setDirChildren((prev) => ({ ...prev, [entry.path]: children }))
    } catch { /* show empty on error */ }
    setExpandedDirs((prev) => { const s = new Set(prev); s.add(entry.path); return s })
  }

  async function handleOpenFile(entry: WorkspaceFileEntry) {
    if (!workspace || fileLoading) return
    setFileLoading(entry.path)
    try {
      const content = await apiReadWorkspaceFile(workspace.id, entry.path)
      setViewingFile({ path: entry.path, content })
    } catch { /* ignore */ }
    finally { setFileLoading(null) }
  }

  function renderEntries(entries: WorkspaceFileEntry[], depth: number) {
    return entries.map((entry) => (
      <div key={entry.path}>
        <button
          type="button"
          onClick={() => void (entry.is_dir ? handleToggleDir(entry) : handleOpenFile(entry))}
          className={`w-full flex items-center gap-1 py-0.5 text-left text-[11px] rounded hover:bg-surface-2 transition-colors truncate ${
            fileLoading === entry.path ? 'opacity-50' : ''
          } ${viewingFile?.path === entry.path ? 'bg-surface-2 text-text-primary' : 'text-text-secondary'}`}
          style={{ paddingLeft: `${0.5 + depth * 0.875}rem`, paddingRight: '0.5rem' }}
        >
          {entry.is_dir
            ? expandedDirs.has(entry.path)
              ? <ChevronDown className="w-3 h-3 shrink-0 text-text-muted" />
              : <ChevronRight className="w-3 h-3 shrink-0 text-text-muted" />
            : <span className="w-3 h-3 shrink-0" />
          }
          {entry.is_dir
            ? <Folder className="w-3 h-3 shrink-0 text-text-muted ml-0.5" />
            : <File className="w-3 h-3 shrink-0 text-text-muted ml-0.5" />
          }
          <span className="truncate ml-1">{entry.name}</span>
        </button>
        {entry.is_dir && expandedDirs.has(entry.path) && dirChildren[entry.path] && (
          renderEntries(dirChildren[entry.path], depth + 1)
        )}
      </div>
    ))
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

  const hasActivity = activityStream.length > 0

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
        <Terminal className="w-4 h-4 text-text-muted" />
        <h1 className="text-sm font-semibold text-text-primary">TRINITY CODE</h1>
        {workspace.language_hint && (
          <span className="text-xs text-text-muted bg-surface-2 px-2 py-0.5 rounded-md">{workspace.language_hint}</span>
        )}
        {!workspace.root_path && (
          <span className="text-xs text-text-muted italic ml-auto">root_path não configurado</span>
        )}
      </div>

      {/* Main area */}
      <div className="flex-1 flex overflow-hidden min-h-0">

        {/* File tree sidebar */}
        {workspace.root_path && (
          <div className="w-48 shrink-0 border-r border-border overflow-y-auto bg-surface-1 py-2">
            {treeError ? (
              <p className="text-[11px] text-text-muted px-3 py-2">{treeError}</p>
            ) : treeRoot.length === 0 ? (
              <p className="text-[11px] text-text-muted px-3 py-2 italic">Diretório vazio</p>
            ) : (
              renderEntries(treeRoot, 0)
            )}
          </div>
        )}

        {/* Right: file viewer + activity stream + terminal */}
        <div className="flex-1 flex flex-col min-w-0 overflow-hidden">

          {/* File viewer (shown when a file is open) */}
          {viewingFile && (
            <div className="flex-1 flex flex-col overflow-hidden border-b border-border min-h-0">
              <div className="flex items-center justify-between px-4 py-1.5 bg-surface-1 border-b border-border shrink-0">
                <span className="text-[11px] text-text-muted font-mono truncate">{viewingFile.path}</span>
                <button
                  type="button"
                  onClick={() => setViewingFile(null)}
                  className="p-0.5 rounded text-text-muted hover:text-text-primary hover:bg-surface-2 transition-colors ml-3 shrink-0"
                  title="Fechar arquivo"
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              </div>
              <pre className="flex-1 overflow-auto p-4 font-mono text-xs text-text-secondary leading-relaxed whitespace-pre bg-[#0a0a0a]">
                {viewingFile.content}
              </pre>
            </div>
          )}

          {/* Activity Stream — shown when task has run or is running */}
          {hasActivity && (
            <div className="shrink-0 border-b border-border">
              <div className="flex items-center gap-2 px-4 py-1.5 bg-surface-1 border-b border-border">
                <Zap className="w-3 h-3 text-accent-light" />
                <span className="text-[11px] font-medium text-text-muted">Activity Stream</span>
                {taskRunning && (
                  <div className="w-2 h-2 bg-accent rounded-full animate-pulse ml-auto" />
                )}
              </div>
              <div
                ref={activityRef}
                className="max-h-28 overflow-y-auto px-4 py-2 space-y-1 bg-surface-1"
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

          {/* Task result / error banner */}
          {taskResult && !taskRunning && (
            <div className="shrink-0 border-b border-border px-4 py-2 bg-surface-1 flex items-start gap-3">
              <div className="flex-1 min-w-0">
                <p className="text-xs font-medium text-green-400 mb-0.5">Tarefa concluída · {taskResult.iterations} iteração{taskResult.iterations !== 1 ? 'ões' : ''} · {taskResult.duration_ms}ms</p>
                {taskResult.answer && (
                  <p className="text-[11px] text-text-secondary whitespace-pre-wrap leading-relaxed">{taskResult.answer}</p>
                )}
              </div>
              <button type="button" onClick={() => setTaskResult(null)} className="shrink-0 text-text-muted hover:text-text-primary">
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
          {taskError && !taskRunning && (
            <div className="shrink-0 border-b border-border px-4 py-2 bg-surface-1 flex items-center gap-2">
              <p className="flex-1 text-[11px] text-red-400">{taskError}</p>
              <button type="button" onClick={() => setTaskError('')} className="shrink-0 text-text-muted hover:text-text-primary">
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          )}

          {/* Terminal output */}
          <div
            ref={outputRef}
            className={`${viewingFile ? 'h-32 shrink-0' : 'flex-1'} overflow-y-auto bg-[#0d0d0d] px-5 py-4 font-mono text-xs leading-relaxed text-green-400`}
          >
            {output.length === 0 && !running ? (
              <span className="text-text-muted">Pronto. Digite um comando abaixo ou descreva uma tarefa para o assistente.</span>
            ) : (
              output.map((line, i) => (
                <div key={i} className="whitespace-pre-wrap break-all">{line}</div>
              ))
            )}
            {running && (
              <div className="flex items-center gap-1.5 text-text-muted mt-1">
                <div className="w-2 h-2 bg-accent rounded-full animate-pulse" />
                <span>Executando…</span>
              </div>
            )}
            {lastResult && !running && (
              <div className={`mt-2 text-[11px] ${lastResult.success ? 'text-green-500' : 'text-red-400'}`}>
                Saiu com código {lastResult.exit_code} · {lastResult.duration_ms}ms
              </div>
            )}
          </div>

          {/* Shell command input — inside terminal section */}
          <div className="shrink-0 border-t border-border px-4 py-2 flex items-center gap-3 bg-[#0d0d0d]">
            <span className="text-text-muted font-mono text-xs select-none">$</span>
            <input
              type="text"
              value={command}
              onChange={(e) => setCommand(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={running || taskRunning}
              placeholder="ls -la"
              className="flex-1 bg-transparent text-xs text-green-400 font-mono placeholder:text-text-muted outline-none disabled:opacity-50"
            />
            <button
              type="button"
              onClick={running ? undefined : () => void handleRun()}
              disabled={(!command.trim() && !running) || taskRunning}
              className="p-1 rounded transition-colors disabled:opacity-30"
              title={running ? 'Executando' : 'Executar comando'}
            >
              {running
                ? <Square className="w-3.5 h-3.5 text-text-muted" />
                : <Play className="w-3.5 h-3.5 text-text-muted" />
              }
            </button>
          </div>
        </div>
      </div>

      {/* Task input — primary bottom input for AI task */}
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
            title="Executar tarefa com IA (Ctrl+Enter)"
          >
            {taskRunning
              ? <><div className="w-3 h-3 border border-white border-t-transparent rounded-full animate-spin" /><span>Executando</span></>
              : <><Zap className="w-3.5 h-3.5" /><span>Executar</span></>
            }
          </button>
        </div>
      </div>
    </div>
  )
}
