'use client'

import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { AgentStep } from '@/types'
import { BrandSymbol } from '@/components/brand/master'
import { ChevronDown, ChevronUp, CheckCircle2, XCircle, Copy, Check, Pencil, Volume2, VolumeX, Loader2 } from 'lucide-react'
import { apiSynthesizeSpeech } from '@/api/client'
import { MediaCard } from '@/components/chat/media-card'
import { JobFileCard } from '@/components/chat/job-file-card'
import { t } from '@/i18n'

/** An agent step is shown by what it does, never by an internal tool name. */
function stepLabel(step: AgentStep): string {
  const translated = t(step.activity)
  return translated !== step.activity ? translated : (step.label ?? t('activity.tool.generic'))
}

interface MessageBubbleProps {
  message: {
    id: string
    role: string
    content: string
    isStreaming?: boolean
    media_job_id?: string | null
    execution_job_id?: string | null
  }
  agentSteps?: AgentStep[]
  statusMessages?: string[]
  onEdit?: (newContent: string) => void
  /** Read this answer aloud once, as soon as it is finished (Settings → Voz → Ler respostas em voz alta). */
  autoRead?: boolean
}

export function MessageBubble({ message, agentSteps = [], statusMessages = [], onEdit, autoRead = false }: MessageBubbleProps) {
  const isUser = message.role === 'user'
  const [copied, setCopied] = useState(false)
  const [editing, setEditing] = useState(false)
  const [editText, setEditText] = useState(message.content)
  const [isSynthesizing, setIsSynthesizing] = useState(false)
  const [isPlaying, setIsPlaying] = useState(false)
  const [playError, setPlayError] = useState<string | null>(null)
  // Synthesis costs a provider call and TCU, so the audio for a given
  // message is generated once and reused for every later replay.
  const audioUrlRef = useRef<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)

  useEffect(() => {
    return () => {
      audioRef.current?.pause()
      if (audioUrlRef.current) URL.revokeObjectURL(audioUrlRef.current)
    }
  }, [])

  // The same read-aloud as the "Ouvir" button, started once for an answer that just finished.
  const autoReadDoneRef = useRef(false)
  useEffect(() => {
    if (!autoRead || autoReadDoneRef.current || isUser || message.isStreaming || !message.content.trim()) return
    autoReadDoneRef.current = true
    void handlePlayAudio()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoRead, message.isStreaming, message.content])

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(message.content)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // clipboard unavailable — silently ignore
    }
  }

  async function handlePlayAudio() {
    // Second click while it is talking = stop.
    if (isPlaying) {
      audioRef.current?.pause()
      setIsPlaying(false)
      return
    }
    if (isSynthesizing) return

    setPlayError(null)
    try {
      // Reuse the audio if this message was already read aloud.
      if (!audioUrlRef.current) {
        setIsSynthesizing(true)
        const blob = await apiSynthesizeSpeech(message.content)
        if (blob.size === 0) throw new Error('empty audio payload')
        audioUrlRef.current = URL.createObjectURL(blob)
      }

      const audio = audioRef.current ?? new Audio()
      audioRef.current = audio
      audio.src = audioUrlRef.current
      audio.onended = () => setIsPlaying(false)
      audio.onpause = () => setIsPlaying(false)

      await audio.play()
      setIsPlaying(true)
    } catch (err) {
      // The raw DOM message here is unhelpful to a person ("Failed to load
      // because no supported source was found" is what a CSP block on
      // blob: media looks like from the outside). Keep the technical detail
      // in the console for diagnosis and tell the user something true and
      // actionable instead.
      console.error('[voice] read-aloud failed', err)
      // A source that will not load will not load on retry either — drop it
      // so the next click re-synthesises rather than replaying a dud.
      if (audioUrlRef.current) {
        URL.revokeObjectURL(audioUrlRef.current)
        audioUrlRef.current = null
      }
      setIsPlaying(false)
      setPlayError('Não foi possível reproduzir o áudio. Tente novamente.')
    } finally {
      setIsSynthesizing(false)
    }
  }

  function handleEditConfirm() {
    const trimmed = editText.trim()
    if (!trimmed || trimmed === message.content) {
      setEditing(false)
      setEditText(message.content)
      return
    }
    setEditing(false)
    onEdit?.(trimmed)
  }

  function handleEditKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleEditConfirm()
    }
    if (e.key === 'Escape') {
      setEditing(false)
      setEditText(message.content)
    }
  }

  if (isUser) {
    return (
      <div className="flex justify-end animate-fadeIn group/user-msg">
        <div className="max-w-[78%]">
          {editing ? (
            <div className="bg-surface-2 border border-accent/40 rounded-2xl rounded-tr-sm px-4 py-3 space-y-2">
              <textarea
                className="w-full bg-transparent text-sm text-text-primary leading-relaxed resize-none outline-none min-h-[60px]"
                value={editText}
                onChange={(e) => setEditText(e.target.value)}
                onKeyDown={handleEditKeyDown}
                autoFocus
                rows={Math.max(2, editText.split('\n').length)}
              />
              <div className="flex gap-2 justify-end">
                <button
                  onClick={() => { setEditing(false); setEditText(message.content) }}
                  className="text-xs text-text-muted hover:text-text-secondary transition-colors px-2 py-1"
                >
                  Cancelar
                </button>
                <button
                  onClick={handleEditConfirm}
                  className="text-xs bg-accent text-text-inverse rounded-lg px-3 py-1 hover:bg-accent/90 transition-colors"
                >
                  Enviar
                </button>
              </div>
            </div>
          ) : (
            <>
              <div className="bg-surface-2 border border-border rounded-2xl rounded-tr-sm px-4 py-3">
                <p className="text-sm text-text-primary whitespace-pre-wrap leading-relaxed">
                  {message.content}
                </p>
              </div>
              <div className="mt-1 flex justify-end gap-3 opacity-0 group-hover/user-msg:opacity-100 transition-opacity">
                <button
                  onClick={handleCopy}
                  className="flex items-center gap-1 text-xs text-text-muted hover:text-text-secondary transition-colors"
                  aria-label={copied ? 'Copiado' : 'Copiar mensagem'}
                >
                  {copied ? (
                    <Check className="w-3 h-3 text-success" />
                  ) : (
                    <Copy className="w-3 h-3" />
                  )}
                  <span>{copied ? 'Copiado!' : 'Copiar'}</span>
                </button>
                {onEdit && (
                  <button
                    onClick={() => { setEditing(true); setEditText(message.content) }}
                    className="flex items-center gap-1 text-xs text-text-muted hover:text-text-secondary transition-colors"
                    aria-label="Editar mensagem"
                  >
                    <Pencil className="w-3 h-3" />
                    <span>Editar</span>
                  </button>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    )
  }

  // Assistant
  const hasContent = !!message.content
  const showAgentSteps = agentSteps.length > 0 && !hasContent

  return (
    <div className="flex gap-3 animate-fadeIn group/msg">
      {/* The official symbol alone speaks for TRINDATRON: no name beside it. */}
      <div className="shrink-0 mt-0.5 w-8 flex justify-center">
        <BrandSymbol size={30} />
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0 pt-1.5">

        {/* Agent steps (only while waiting for final answer) */}
        {showAgentSteps && (
          <div className="mb-3 space-y-1.5">
            {agentSteps.map((step, i) => (
              <AgentStepCard key={i} step={step} />
            ))}
          </div>
        )}

        {/* A creation this reply started */}
        {message.media_job_id && <MediaCard jobId={message.media_job_id} />}
        {/* A job this reply started (a presentation): progress, then the file */}
        {message.execution_job_id && <JobFileCard jobId={message.execution_job_id} />}

        {/* Main content */}
        {hasContent && (
          <div className="prose-trinity text-sm text-text-primary max-w-none">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {message.content}
            </ReactMarkdown>
          </div>
        )}

        {/* Streaming cursor */}
        {message.isStreaming && !hasContent && agentSteps.length === 0 && statusMessages.length === 0 && (
          <span className="inline-flex items-center gap-1 text-sm text-text-muted">
            <span className="inline-block w-0.5 h-4 bg-accent-light animate-blink" />
          </span>
        )}

        {message.isStreaming && hasContent && (
          <span className="inline-block w-0.5 h-4 bg-accent-light ml-0.5 animate-blink align-middle" />
        )}

        {/* Copy / play-audio buttons — only for finished assistant messages with content */}
        {hasContent && !message.isStreaming && (
          <div className="mt-2 flex items-center gap-4 opacity-0 group-hover/msg:opacity-100 transition-opacity">
            <button
              onClick={handleCopy}
              className="flex items-center gap-1.5 text-xs text-text-muted hover:text-text-secondary transition-colors"
              aria-label={copied ? 'Copiado' : 'Copiar mensagem'}
            >
              {copied ? (
                <Check className="w-3.5 h-3.5 text-success" />
              ) : (
                <Copy className="w-3.5 h-3.5" />
              )}
              <span>{copied ? 'Copiado!' : 'Copiar'}</span>
            </button>
            <button
              onClick={handlePlayAudio}
              disabled={isSynthesizing}
              className="flex items-center gap-1.5 text-xs text-text-muted hover:text-text-secondary transition-colors disabled:opacity-50"
              aria-label={isPlaying ? 'Parar leitura' : 'Ouvir resposta'}
            >
              {isSynthesizing ? (
                <Loader2 className="w-3.5 h-3.5 animate-spin" />
              ) : isPlaying ? (
                <VolumeX className="w-3.5 h-3.5" />
              ) : (
                <Volume2 className="w-3.5 h-3.5" />
              )}
              <span>{isSynthesizing ? 'Gerando áudio…' : isPlaying ? 'Parar' : 'Ouvir'}</span>
            </button>
            {playError && (
              <span role="status" className="text-xs text-warning">{playError}</span>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function AgentStepCard({ step }: { step: AgentStep }) {
  const [expanded, setExpanded] = useState(false)

  if (step.type === 'tool_call') {
    return (
      <div className="bg-surface-1 border border-border rounded-lg overflow-hidden">
        <button
          onClick={() => setExpanded((v) => !v)}
          className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-left text-text-secondary hover:text-text-primary transition-colors"
        >
          <span className="font-medium flex-1">{stepLabel(step)}</span>
          {expanded ? (
            <ChevronUp className="w-3 h-3 shrink-0" />
          ) : (
            <ChevronDown className="w-3 h-3 shrink-0" />
          )}
        </button>
        {expanded && step.args && (
          <pre className="px-3 pb-2 text-[11px] text-text-muted overflow-x-auto leading-relaxed border-t border-border font-mono">
            {JSON.stringify(step.args, null, 2)}
          </pre>
        )}
      </div>
    )
  }

  if (step.type === 'tool_result') {
    const ok = step.success !== false
    return (
      <div className={`border rounded-lg overflow-hidden ${ok ? 'bg-surface-1 border-border' : 'bg-error/5 border-error/20'}`}>
        <button
          onClick={() => setExpanded((v) => !v)}
          className="w-full flex items-center gap-2.5 px-3 py-2 text-xs text-left hover:opacity-80 transition-opacity"
        >
          {ok ? (
            <CheckCircle2 className="w-3.5 h-3.5 text-success shrink-0" />
          ) : (
            <XCircle className="w-3.5 h-3.5 text-error shrink-0" />
          )}
          <span className="text-text-secondary flex-1">{stepLabel(step)}</span>
          {expanded ? (
            <ChevronUp className="w-3 h-3 text-text-muted shrink-0" />
          ) : (
            <ChevronDown className="w-3 h-3 text-text-muted shrink-0" />
          )}
        </button>
        {expanded && (
          <pre className="px-3 pb-2 text-[11px] text-text-muted overflow-x-auto whitespace-pre-wrap leading-relaxed border-t border-border font-mono">
            {step.output ?? '(sem output)'}
          </pre>
        )}
      </div>
    )
  }

  return null
}
