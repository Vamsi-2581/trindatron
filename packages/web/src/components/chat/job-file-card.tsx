'use client'

import { useEffect, useState } from 'react'
import { AlertTriangle, Download, FileText, Loader2 } from 'lucide-react'
import {
  apiFetchExecutionArtifactBlob,
  apiGetExecutionArtifactLink,
  apiGetExecutionJob,
  apiListExecutionArtifacts,
  type ExecutionArtifact,
} from '@/api/client'
import type { ExecutionJob } from '@/types'

/** A job is over once it reaches one of these; anything else is still being made. */
const FINISHED = new Set(['completed', 'failed', 'cancelled', 'expired', 'cleanup', 'destroyed'])
const POLL_MS = 5000

function sizeLabel(bytes: number): string {
  return bytes >= 1_000_000 ? `${(bytes / 1_000_000).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1000))} KB`
}

function saveFrom(url: string, filename: string) {
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.rel = 'noreferrer'
  document.body.appendChild(link)
  link.click()
  link.remove()
}

/**
 * A job the conversation started (a presentation, today): followed until it finishes, then its files are
 * offered for download. Files are private to their owner, so each download asks the Core for a short-lived
 * link; where storage cannot sign one (local development) the file is read with the session instead.
 */
export function JobFileCard({ jobId }: { jobId: string }) {
  const [job, setJob] = useState<ExecutionJob | null>(null)
  const [files, setFiles] = useState<ExecutionArtifact[] | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [saving, setSaving] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    async function follow() {
      try {
        const next = await apiGetExecutionJob(jobId)
        if (cancelled) return
        setJob(next)
        if (!FINISHED.has(next.status)) {
          timer = setTimeout(follow, POLL_MS)
          return
        }
        if (next.status === 'completed') {
          const produced = await apiListExecutionArtifacts(jobId)
          if (!cancelled) setFiles(produced)
        }
      } catch {
        if (!cancelled) timer = setTimeout(follow, POLL_MS)
      }
    }
    void follow()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [jobId])

  async function download(file: ExecutionArtifact) {
    setSaving(file.id)
    setProblem(null)
    try {
      const link = await apiGetExecutionArtifactLink(file.id)
      if (link.url) {
        saveFrom(link.url, file.filename)
      } else {
        const url = URL.createObjectURL(await apiFetchExecutionArtifactBlob(file.id))
        saveFrom(url, file.filename)
        setTimeout(() => URL.revokeObjectURL(url), 60_000)
      }
    } catch {
      setProblem('Não foi possível baixar o arquivo agora. Tente de novo em instantes.')
    } finally {
      setSaving(null)
    }
  }

  const running = !job || !FINISHED.has(job.status)
  const failed = job !== null && FINISHED.has(job.status) && job.status !== 'completed'

  return (
    <div className="mb-3 max-w-md rounded-xl border border-border bg-surface-1 px-3 py-3" data-testid="job-file-card">
      {running && (
        <div className="flex items-center gap-2 text-xs text-text-secondary" role="status">
          <Loader2 className="w-4 h-4 animate-spin" />
          <span>Criando sua apresentação… isso leva alguns minutos.</span>
        </div>
      )}

      {failed && (
        <div className="flex items-center gap-2 text-xs text-text-secondary" role="alert">
          <AlertTriangle className="w-4 h-4 text-error" />
          <span>Não foi possível criar a apresentação. Nada foi cobrado por ela.</span>
        </div>
      )}

      {job?.status === 'completed' && files !== null && (
        files.length === 0 ? (
          <p className="text-xs text-text-secondary">A apresentação terminou, mas nenhum arquivo ficou disponível.</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {files.map((file) => (
              <li key={file.id} className="flex items-center gap-3">
                <FileText className="w-5 h-5 text-text-muted shrink-0" />
                <span className="flex-1 min-w-0 text-xs text-text-primary truncate" title={file.filename}>
                  {file.filename} <span className="text-text-muted">· {sizeLabel(file.size_bytes)}</span>
                </span>
                <button
                  type="button"
                  onClick={() => void download(file)}
                  disabled={saving === file.id}
                  className="flex items-center gap-1 text-xs text-text-muted hover:text-text-primary disabled:opacity-50"
                >
                  {saving === file.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Download className="w-3.5 h-3.5" />}
                  Baixar
                </button>
              </li>
            ))}
          </ul>
        )
      )}

      {problem && <p className="mt-2 text-xs text-error">{problem}</p>}
    </div>
  )
}
