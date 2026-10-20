'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { COMPOSE_EVENT } from './media-card'
import { AlertCircle, ArrowUp, Film, ImageIcon, Loader2, Mic, MicOff, Paperclip, RotateCw, X } from 'lucide-react'
import {
  apiAttachFile,
  apiGetCapabilities,
  apiGetPreferences,
  apiListAttachments,
  apiRemoveAttachment,
  apiTranscribeAudio,
  type Capabilities,
  type SessionAttachment,
} from '@/api/client'

/** The Core refuses anything larger; checking first avoids uploading a file only to have it refused. */
const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
const ACCEPT = 'image/*,video/*,application/pdf,text/plain,text/markdown,text/csv,application/json,.md,.csv,.txt,.json'

interface PendingFile {
  id: string
  file: File
  status: 'uploading' | 'failed'
  error: string | null
}

interface ComposerProps {
  onSend: (message: string) => void
  disabled?: boolean
  placeholder?: string
  /** The conversation files are attached to; the Core reads documents and images into its context. */
  sessionId?: string | null
  /** Before the first message (Home): opens the conversation a file is attached to. */
  ensureSession?: () => Promise<string>
}

export function Composer({ onSend, disabled = false, placeholder = 'Escreva o que precisa fazer…', sessionId = null, ensureSession }: ComposerProps) {
  const [value, setValue] = useState('')
  const [isRecording, setIsRecording] = useState(false)
  const [isTranscribing, setIsTranscribing] = useState(false)
  const [micError, setMicError] = useState<string | null>(null)
  const [micSupported, setMicSupported] = useState(false)
  const [menuOpen, setMenuOpen] = useState(false)
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null)
  const [attached, setAttached] = useState<SessionAttachment[]>([])
  const [pending, setPending] = useState<PendingFile[]>([])
  const [fileError, setFileError] = useState<string | null>(null)
  // Settings → Geral. Until the preference loads, Enter sends — the default for everyone.
  const [sendOnEnter, setSendOnEnter] = useState(true)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<BlobPart[]>([])
  const streamRef = useRef<MediaStream | null>(null)

  useEffect(() => {
    if (typeof window !== 'undefined') {
      setMicSupported(!!window.MediaRecorder && !!navigator.mediaDevices?.getUserMedia)
    }
    // What this account can create right now; nothing is offered that the Core would refuse.
    apiGetCapabilities().then(setCapabilities).catch(() => setCapabilities(null))
    apiGetPreferences().then((preferences) => setSendOnEnter(preferences.send_on_enter)).catch(() => undefined)
  }, [])

  useEffect(() => {
    if (!sessionId) return
    let cancelled = false
    apiListAttachments(sessionId)
      .then((list) => {
        if (!cancelled) setAttached((current) => [...list, ...current.filter((item) => !list.some((known) => known.id === item.id))])
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [sessionId])

  useEffect(() => {
    if (!micError) return
    const t = setTimeout(() => setMicError(null), 5000)
    return () => clearTimeout(t)
  }, [micError])

  useEffect(() => {
    if (!textareaRef.current) return
    textareaRef.current.style.height = 'auto'
    textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 200)}px`
  }, [value])

  const uploading = pending.some((item) => item.status === 'uploading')
  const canSend = value.trim().length > 0 && !disabled && !uploading
  const canCreateImage = capabilities?.image_generation?.available === true
  const canCreateVideo = capabilities?.video_generation?.available === true

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key !== 'Enter' || e.shiftKey || e.nativeEvent.isComposing) return
    const modifier = e.ctrlKey || e.metaKey
    if (sendOnEnter || modifier) {
      e.preventDefault()
      submit()
    }
  }

  function submit() {
    const trimmed = value.trim()
    if (!trimmed || !canSend) return
    onSend(trimmed)
    setValue('')
  }

  const attach = useCallback(
    async (file: File) => {
      const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`
      const fail = (error: string) =>
        setPending((list) => [...list.filter((item) => item.id !== id), { id, file, status: 'failed', error }])
      if (file.size > MAX_ATTACHMENT_BYTES) {
        fail('O arquivo é maior do que o limite de 10 MB.')
        return
      }
      setPending((list) => [...list, { id, file, status: 'uploading', error: null }])
      try {
        const target = sessionId ?? (ensureSession ? await ensureSession() : null)
        if (!target) throw new Error('Não foi possível abrir a conversa para anexar.')
        const created = await apiAttachFile(target, file)
        setPending((list) => list.filter((item) => item.id !== id))
        setAttached((list) => [...list.filter((item) => item.id !== created.id), created])
      } catch (err) {
        fail(err instanceof Error ? err.message : 'Não foi possível anexar o arquivo.')
      }
    },
    [sessionId, ensureSession],
  )

  function onFilesChosen(event: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? [])
    event.target.value = ''
    files.forEach((file) => void attach(file))
  }

  async function remove(item: SessionAttachment) {
    setFileError(null)
    try {
      await apiRemoveAttachment(item.session_id, item.id)
      setAttached((list) => list.filter((entry) => entry.id !== item.id))
    } catch (err) {
      setFileError(err instanceof Error ? err.message : 'Não foi possível remover o arquivo.')
    }
  }

  function startCreation(prefix: string) {
    setMenuOpen(false)
    setValue((current) => (current.trim() ? `${prefix}${current.trim()}` : prefix))
    requestAnimationFrame(() => {
      const el = textareaRef.current
      if (!el) return
      el.focus()
      el.setSelectionRange(el.value.length, el.value.length)
    })
  }

  // "Criar outra" on a creation card starts the next request here, in the same conversation.
  useEffect(() => {
    function onCompose(event: Event) {
      const text = (event as CustomEvent<{ text?: string }>).detail?.text
      if (typeof text === 'string' && text) startCreation(text)
    }
    window.addEventListener(COMPOSE_EVENT, onCompose)
    return () => window.removeEventListener(COMPOSE_EVENT, onCompose)
  })

  function stopStream() {
    streamRef.current?.getTracks().forEach(track => track.stop())
    streamRef.current = null
  }

  async function startRecording() {
    setMicError(null)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream
      chunksRef.current = []

      const recorder = new MediaRecorder(stream)
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data)
      }
      recorder.onstop = async () => {
        stopStream()
        const blob = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' })
        chunksRef.current = []
        if (blob.size === 0) return
        setIsTranscribing(true)
        try {
          const result = await apiTranscribeAudio(blob, `clip.${(recorder.mimeType || 'audio/webm').split('/')[1]?.split(';')[0] || 'webm'}`)
          if (result.text) {
            setValue(prev => prev ? `${prev} ${result.text}` : result.text)
            textareaRef.current?.focus()
          }
        } catch (err) {
          setMicError(err instanceof Error ? err.message : 'Falha ao transcrever áudio')
        } finally {
          setIsTranscribing(false)
        }
      }

      mediaRecorderRef.current = recorder
      recorder.start()
      setIsRecording(true)
    } catch {
      setMicError('Não foi possível acessar o microfone')
      stopStream()
    }
  }

  function toggleMic() {
    if (isRecording) {
      mediaRecorderRef.current?.stop()
      setIsRecording(false)
      return
    }
    startRecording()
  }

  useEffect(() => () => stopStream(), [])

  const failed = pending.find((item) => item.status === 'failed')
  const notice = micError ?? failed?.error ?? fileError

  return (
    <div className="flex flex-col gap-2">
      {(attached.length > 0 || pending.length > 0) && (
        <div className="flex flex-wrap gap-2" data-testid="composer-attachments">
          {attached.map((item) => (
            <span key={item.id} className="flex items-center gap-1.5 max-w-full rounded-full border border-accent/30 bg-accent/10 pl-3 pr-1 py-1 text-xs text-accent-light">
              <Paperclip className="w-3 h-3 shrink-0" />
              <span className="truncate">{item.readable ? item.name : `${item.name} (conteúdo não lido)`}</span>
              <button type="button" onClick={() => void remove(item)} aria-label={`Remover ${item.name}`} className="p-1 rounded-full hover:bg-accent/20">
                <X className="w-3 h-3" />
              </button>
            </span>
          ))}
          {pending.map((item) => (
            <span
              key={item.id}
              className={[
                'flex items-center gap-1.5 max-w-full rounded-full border pl-3 pr-1 py-1 text-xs',
                item.status === 'failed' ? 'border-error/40 bg-error/5 text-error' : 'border-border bg-surface-2 text-text-secondary',
              ].join(' ')}
            >
              {item.status === 'uploading' ? <Loader2 className="w-3 h-3 animate-spin shrink-0" /> : <AlertCircle className="w-3 h-3 shrink-0" />}
              <span className="truncate">{item.status === 'uploading' ? `Enviando ${item.file.name}…` : item.file.name}</span>
              {item.status === 'failed' && (
                <button
                  type="button"
                  onClick={() => {
                    setPending((list) => list.filter((entry) => entry.id !== item.id))
                    void attach(item.file)
                  }}
                  aria-label={`Tentar enviar ${item.file.name} de novo`}
                  className="p-1 rounded-full hover:bg-error/10"
                >
                  <RotateCw className="w-3 h-3" />
                </button>
              )}
              {item.status === 'failed' && (
                <button
                  type="button"
                  onClick={() => setPending((list) => list.filter((entry) => entry.id !== item.id))}
                  aria-label={`Descartar ${item.file.name}`}
                  className="p-1 rounded-full hover:bg-error/10"
                >
                  <X className="w-3 h-3" />
                </button>
              )}
            </span>
          ))}
        </div>
      )}

      <div
        className={[
          'relative bg-surface-2 border rounded-2xl transition-colors',
          // A disabled state is drawn, never faded. `opacity-60` on the
          // wrapper dimmed the placeholder and the typed text along with the
          // chrome, taking both under the contrast floor — the composer is
          // where the product is used, and it has to stay readable even when
          // it is momentarily not accepting input.
          disabled
            ? 'border-border-subtle bg-surface-1'
            : 'border-border focus-within:border-accent/50',
        ].join(' ')}
      >
        {menuOpen && (
          <div role="menu" className="absolute bottom-full left-2 mb-2 w-56 rounded-xl border border-border bg-surface-1 shadow-lg py-1 z-10">
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setMenuOpen(false)
                fileInputRef.current?.click()
              }}
              className="w-full flex items-center gap-2 px-3 py-2 text-sm text-text-secondary hover:bg-surface-2 hover:text-text-primary"
            >
              <Paperclip className="w-4 h-4" /> Anexar imagem, vídeo ou documento
            </button>
            {canCreateImage && (
              <button
                type="button"
                role="menuitem"
                onClick={() => startCreation('Crie uma imagem de ')}
                className="w-full flex items-center gap-2 px-3 py-2 text-sm text-text-secondary hover:bg-surface-2 hover:text-text-primary"
              >
                <ImageIcon className="w-4 h-4" /> Criar imagem
              </button>
            )}
            {canCreateVideo && (
              <button
                type="button"
                role="menuitem"
                onClick={() => startCreation('Crie um vídeo de ')}
                className="w-full flex items-center gap-2 px-3 py-2 text-sm text-text-secondary hover:bg-surface-2 hover:text-text-primary"
              >
                <Film className="w-4 h-4" /> Criar vídeo
              </button>
            )}
          </div>
        )}

        <input ref={fileInputRef} type="file" accept={ACCEPT} multiple className="hidden" onChange={onFilesChosen} data-testid="composer-file-input" />

        <div className="flex items-end gap-2 px-4 py-3">
          <button
            type="button"
            onClick={() => setMenuOpen((open) => !open)}
            disabled={disabled}
            title="Anexar ou criar"
            aria-label="Anexar ou criar"
            aria-expanded={menuOpen}
            className="shrink-0 mb-0.5 p-1.5 rounded-lg text-text-muted hover:text-text-secondary disabled:cursor-not-allowed"
          >
            <Paperclip className="w-4 h-4" />
          </button>

          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={placeholder}
            disabled={disabled}
            rows={1}
            className="flex-1 bg-transparent text-sm text-text-primary placeholder:text-text-muted resize-none outline-none leading-relaxed min-h-[24px] max-h-[200px] py-0.5"
          />

          {micSupported ? (
            <button
              type="button"
              onClick={toggleMic}
              disabled={disabled || isTranscribing}
              title={isRecording ? 'Parar gravação' : isTranscribing ? 'Transcrevendo…' : 'Gravar voz'}
              aria-label={isRecording ? 'Parar gravação' : 'Entrada de voz'}
              className={[
                'shrink-0 mb-0.5 p-1.5 rounded-lg transition-colors',
                isRecording
                  ? 'text-red-400 bg-red-400/10 animate-pulse'
                  : 'text-text-muted hover:text-text-secondary',
                (disabled || isTranscribing) ? 'text-text-muted cursor-not-allowed' : 'cursor-pointer',
              ].join(' ')}
            >
              {isTranscribing ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : isRecording ? (
                <MicOff className="w-4 h-4" />
              ) : (
                <Mic className="w-4 h-4" />
              )}
            </button>
          ) : (
            <button
              type="button"
              disabled
              title="Voz não suportada neste navegador"
              aria-label="Entrada de voz indisponível"
              className="shrink-0 mb-0.5 p-1.5 rounded-lg text-text-muted cursor-not-allowed"
            >
              <Mic className="w-4 h-4" />
            </button>
          )}

          <button
            type="button"
            onClick={submit}
            disabled={!canSend}
            aria-label="Enviar"
            className={[
              'shrink-0 mb-0.5 w-8 h-8 rounded-xl flex items-center justify-center transition-all duration-150',
              canSend
                ? 'bg-accent hover:bg-accent-hover text-text-inverse'
                : 'bg-surface-3 text-text-muted cursor-not-allowed',
            ].join(' ')}
          >
            <ArrowUp className="w-4 h-4" />
          </button>
        </div>
      </div>

      {notice ? (
        <p role="status" className="text-center text-[11px] text-red-400 leading-none">{notice}</p>
      ) : (
        <p className="text-center text-[11px] text-text-muted leading-none">
          TRINDATRON decide como executar seu trabalho
        </p>
      )}
    </div>
  )
}
