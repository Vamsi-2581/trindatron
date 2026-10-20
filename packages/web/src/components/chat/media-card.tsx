'use client'

import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Download, ExternalLink, Plus, Share2 } from 'lucide-react'
import { apiFetchMediaBlob, apiGetMediaJob, apiGetMediaLink, type MediaJob } from '@/api/client'
import { BrandSymbol } from '@/components/brand/master'

const TERMINAL = new Set(['completed', 'failed'])
const EXTENSIONS: Record<string, string> = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'video/mp4': 'mp4', 'video/webm': 'webm' }

/** The composer listens for this to start the next creation from the conversation. */
export const COMPOSE_EVENT = 'trindatron:compose'

interface FileLinks {
  view: string
  download: string
  /** A blob URL made here (local development); revoked when the card goes away. */
  local: boolean
}

function downloadFrom(url: string, filename: string) {
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
}

/** While a creation is made: the TRINDATRON symbol, alive — never a bar, a percentage or a time left. */
function Creating({ kind }: { kind: string }) {
  const label = kind === 'video' ? 'Criando seu vídeo' : 'Criando sua imagem'
  return (
    <div className="aspect-video flex flex-col items-center justify-center gap-4 bg-surface-2" role="status" aria-label={label}>
      <style>{`
        @keyframes trd-breathe { 0%, 100% { transform: scale(0.94); opacity: 0.82 } 50% { transform: scale(1.04); opacity: 1 } }
        @keyframes trd-orbit { to { transform: rotate(360deg) } }
        @keyframes trd-glow { 0%, 100% { opacity: 0.25 } 50% { opacity: 0.6 } }
        .trd-breathe { animation: trd-breathe 2.4s ease-in-out infinite }
        .trd-orbit { animation: trd-orbit 3.2s linear infinite }
        .trd-glow { animation: trd-glow 2.4s ease-in-out infinite }
        @media (prefers-reduced-motion: reduce) { .trd-breathe, .trd-orbit, .trd-glow { animation: none } }
      `}</style>
      <div className="relative w-24 h-24 flex items-center justify-center">
        <div className="trd-glow absolute inset-2 rounded-full bg-accent blur-2xl" />
        <div
          className="trd-orbit absolute inset-0 rounded-full"
          style={{
            background: 'conic-gradient(from 0deg, transparent 0deg, rgba(34, 211, 238, 0.9) 70deg, transparent 140deg)',
            WebkitMask: 'radial-gradient(farthest-side, transparent calc(100% - 2px), #000 calc(100% - 2px))',
            mask: 'radial-gradient(farthest-side, transparent calc(100% - 2px), #000 calc(100% - 2px))',
          }}
        />
        <BrandSymbol size={52} className="trd-breathe relative" />
      </div>
      <p className="text-xs text-text-secondary tracking-wide">{label}…</p>
    </div>
  )
}

/**
 * A creation asked for in the conversation: followed until it is ready, then shown in place with open and
 * download. The file is private to its owner: the Core hands this card short-lived links, which a media
 * element and a link load straight from storage (no CORS involved, unlike reading it with fetch()).
 */
export function MediaCard({ jobId }: { jobId: string }) {
  const [job, setJob] = useState<MediaJob | null>(null)
  const [links, setLinks] = useState<FileLinks | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [refreshed, setRefreshed] = useState(false)

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    async function follow() {
      try {
        const next = await apiGetMediaJob(jobId)
        if (cancelled) return
        setJob(next)
        setProblem(null)
        if (!TERMINAL.has(next.status)) timer = setTimeout(follow, next.kind === 'video' ? 5000 : 2000)
      } catch {
        if (cancelled) return
        timer = setTimeout(follow, 5000)
      }
    }
    void follow()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [jobId])

  const artifact = job?.status === 'completed' ? job.artifact : null
  const extension = EXTENSIONS[artifact?.content_type ?? ''] ?? (job?.kind === 'video' ? 'mp4' : 'jpg')
  const filename = job ? `trindatron-${job.kind}-${job.id.slice(0, 8)}.${extension}` : 'trindatron'

  const loadLinks = useCallback(async (): Promise<FileLinks | null> => {
    if (!artifact) return null
    const link = await apiGetMediaLink(artifact.id)
    if (link.url) return { view: link.url, download: link.download_url ?? link.url, local: false }
    // No signed links here (local development): read the file with the session instead.
    const url = URL.createObjectURL(await apiFetchMediaBlob(artifact.content_path))
    return { view: url, download: url, local: true }
  }, [artifact])

  useEffect(() => {
    if (!artifact) return
    let cancelled = false
    let made: FileLinks | null = null
    loadLinks()
      .then((next) => {
        if (cancelled || !next) return
        made = next
        setLinks(next)
      })
      .catch(() => {
        if (!cancelled) setProblem('Não foi possível abrir o arquivo agora.')
      })
    return () => {
      cancelled = true
      if (made?.local) URL.revokeObjectURL(made.view)
    }
  }, [artifact, loadLinks])

  /** Links are short-lived: a video left open and played later asks for fresh ones, once. */
  async function refreshLinks() {
    if (refreshed || links?.local) return
    setRefreshed(true)
    try {
      const next = await loadLinks()
      if (next) setLinks(next)
    } catch {
      setProblem('Não foi possível abrir o arquivo agora.')
    }
  }

  async function share() {
    if (!links || !job) return
    try {
      const blob = await (await fetch(links.view)).blob()
      const file = new File([blob], filename, { type: blob.type || artifact?.content_type || '' })
      // The file itself, where the browser can share files; otherwise it is saved so it can be sent anywhere.
      if (typeof navigator.canShare === 'function' && navigator.canShare({ files: [file] })) {
        await navigator.share({ files: [file], title: 'Criado com TRINDATRON' })
        return
      }
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === 'AbortError') return
    }
    downloadFrom(links.download, filename)
  }

  function createAnother() {
    if (!job) return
    const prefix = job.kind === 'video' ? 'Crie outro vídeo de ' : 'Crie outra imagem de '
    window.dispatchEvent(new CustomEvent(COMPOSE_EVENT, { detail: { text: prefix } }))
  }

  const kind = job?.kind === 'video' ? 'Vídeo' : 'Imagem'
  const failed = job?.status === 'failed'
  const ready = job?.status === 'completed' && links !== null

  return (
    <div className="mb-3 max-w-md rounded-xl border border-border bg-surface-1 overflow-hidden" data-testid="media-card">
      {ready ? (
        job.kind === 'video' ? (
          <video src={links.view} controls playsInline className="w-full bg-black" onError={() => void refreshLinks()} />
        ) : (
          // eslint-disable-next-line @next/next/no-img-element -- a private, short-lived link, not an optimisable asset
          <img src={links.view} alt="Imagem criada pelo TRINDATRON" className="w-full" onError={() => void refreshLinks()} />
        )
      ) : failed ? (
        <div className="aspect-video flex items-center justify-center bg-surface-2">
          <AlertTriangle className="w-6 h-6 text-error" />
        </div>
      ) : (
        <Creating kind={job?.kind ?? 'image'} />
      )}

      {job && TERMINAL.has(job.status) && (
        <div className="px-3 py-2 flex items-center gap-3">
          <span className="flex-1 text-xs text-text-secondary">
            {kind} · {failed ? job.error ?? 'Não foi possível criar. Nada foi cobrado.' : 'Pronto'}
          </span>
          {ready && (
            <>
              <a href={links.view} target="_blank" rel="noreferrer" className="flex items-center gap-1 text-xs text-text-muted hover:text-text-primary">
                <ExternalLink className="w-3.5 h-3.5" /> Abrir
              </a>
              <a href={links.download} download={filename} className="flex items-center gap-1 text-xs text-text-muted hover:text-text-primary">
                <Download className="w-3.5 h-3.5" /> Baixar
              </a>
              <button type="button" onClick={() => void share()} className="flex items-center gap-1 text-xs text-text-muted hover:text-text-primary">
                <Share2 className="w-3.5 h-3.5" /> Compartilhar
              </button>
            </>
          )}
          <button type="button" onClick={createAnother} className="flex items-center gap-1 text-xs text-text-muted hover:text-text-primary">
            <Plus className="w-3.5 h-3.5" /> Criar outra
          </button>
        </div>
      )}
      {problem && <p role="status" className="px-3 pb-2 text-[11px] text-warning">{problem}</p>}
    </div>
  )
}
