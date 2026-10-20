'use client'

/**
 * Home: one place to say what you want, and an honest account of what
 * TRINDATRON can do about it right now.
 *
 * Every action below comes from the Core's capability matrix
 * (GET /users/me/capabilities → matrix). Nothing is listed because it would be
 * nice to have, nothing is hidden because it is inconvenient, and no state is
 * decided here: "not in your plan" and "unavailable right now" are the Core's
 * words for the Core's reasons. What replaced the old Home was four fixed
 * prompts and two cards for workspaces that were never built — a page that
 * described a product instead of opening it.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import {
  Bot, CalendarClock, FileText, Image as ImageIcon, MousePointerClick, Mic,
  Presentation, Search, Video, X,
} from 'lucide-react'
import { useAppStore } from '@/store/useAppStore'
import { apiCreateSession, apiGetCapabilityMatrix, apiListAgents, type MatrixEntry } from '@/api/client'
import { Composer } from '@/components/chat/composer'
import { COMPOSE_EVENT } from '@/components/chat/media-card'
import { TrinityMark } from '@/components/brand/trinity-mark'
import type { AgentDefinition } from '@/types'

/**
 * What a person can start from here, and the capability that decides whether
 * they can. `start` is a real action, never decoration:
 *   send    — a complete request the Core can act on as it stands
 *   compose — a request that needs the person's own words or file; the
 *             composer is filled and focused, so they finish it and send
 *   open    — a screen that already exists for this work
 */
interface HomeAction {
  id: string
  capability: string
  label: string
  hint: string
  icon: typeof Search
  start: { kind: 'send' | 'compose'; text: string } | { kind: 'open'; href: string }
}

const ACTIONS: HomeAction[] = [
  {
    id: 'research', capability: 'agent', label: 'Pesquisar agora', icon: Search,
    hint: 'Procura na web e responde com o que encontrou, com as fontes.',
    start: { kind: 'send', text: 'Pesquise na web os últimos avanços em inteligência artificial generativa e cite as fontes.' },
  },
  {
    id: 'image', capability: 'image_generation', label: 'Criar imagem', icon: ImageIcon,
    hint: 'Descreva a imagem e o formato, em qualquer palavra.',
    start: { kind: 'compose', text: 'Crie uma imagem de ' },
  },
  {
    id: 'video', capability: 'video_generation', label: 'Criar vídeo', icon: Video,
    hint: 'Descreva a cena, o movimento e a duração.',
    start: { kind: 'compose', text: 'Crie um vídeo de ' },
  },
  {
    id: 'files', capability: 'file_analysis', label: 'Analisar arquivo', icon: FileText,
    hint: 'Anexe um documento ou imagem e pergunte o que quiser sobre ele.',
    start: { kind: 'compose', text: 'Analise o arquivo que vou anexar e ' },
  },
  {
    id: 'presentation', capability: 'document_generation', label: 'Criar apresentação', icon: Presentation,
    hint: 'Um tema, e o resultado vem como arquivo.',
    start: { kind: 'compose', text: 'Crie uma apresentação sobre ' },
  },
  {
    id: 'voice', capability: 'transcription', label: 'Falar em vez de escrever', icon: Mic,
    hint: 'Use o microfone no campo de mensagem.',
    start: { kind: 'compose', text: '' },
  },
  {
    id: 'computer-use', capability: 'browser_control', label: 'Usar o computador por você', icon: MousePointerClick,
    hint: 'Abre sites, encontra a opção, clica e preenche — pedindo sua aprovação.',
    start: { kind: 'open', href: '/computer-use' },
  },
  {
    id: 'scheduled', capability: 'agent', label: 'Agendar uma tarefa', icon: CalendarClock,
    hint: 'Uma instrução cumprida sozinha, no horário que você escolher.',
    start: { kind: 'open', href: '/scheduled' },
  },
]

const STATUS_NOTE: Record<string, string> = {
  plan: 'Não incluído no seu plano',
  unavailable: 'Indisponível agora',
}

export default function HomePage() {
  const router = useRouter()
  const { prependSession, setPendingMessage } = useAppStore()
  const [matrix, setMatrix] = useState<Record<string, MatrixEntry> | null>(null)
  const [creating, setCreating] = useState(false)
  const [agents, setAgents] = useState<AgentDefinition[]>([])
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null)
  // The conversation opens on the first thing that needs it: a file attached here, or the message.
  const opening = useRef<Promise<string> | null>(null)

  useEffect(() => {
    apiListAgents()
      .then(({ agents }) => setAgents(agents.filter((a) => !a.is_archived)))
      .catch(() => {})
    apiGetCapabilityMatrix().then(setMatrix).catch(() => setMatrix(null))
  }, [])

  const ensureSession = useCallback(() => {
    if (!opening.current) {
      const request = apiCreateSession(undefined, undefined, selectedAgentId ?? undefined).then((session) => {
        prependSession(session)
        return session.id
      })
      request.catch(() => {
        if (opening.current === request) opening.current = null
      })
      opening.current = request
    }
    return opening.current
  }, [selectedAgentId, prependSession])

  const handleSend = useCallback(async (message: string) => {
    if (creating) return
    setCreating(true)
    try {
      const sessionId = await ensureSession()
      setPendingMessage(message)
      router.push(`/chat/${sessionId}`)
    } catch {
      setCreating(false)
    }
  }, [creating, ensureSession, setPendingMessage, router])

  function start(action: HomeAction) {
    if (action.start.kind === 'open') {
      router.push(action.start.href)
      return
    }
    if (action.start.kind === 'send') {
      void handleSend(action.start.text)
      return
    }
    // Fills the composer and puts the cursor at the end: the person finishes the sentence.
    window.dispatchEvent(new CustomEvent(COMPOSE_EVENT, { detail: { text: action.start.text } }))
    document.querySelector<HTMLTextAreaElement>('textarea')?.focus()
  }

  const known = (action: HomeAction) => matrix?.[action.capability]
  const usable = ACTIONS.filter((action) => known(action)?.available)
  const gated = ACTIONS.filter((action) => {
    const status = known(action)?.status
    return status === 'plan' || status === 'unavailable'
  })

  return (
    <div className="flex flex-col h-full items-center justify-center px-6 py-12">
      <div className="w-full max-w-2xl">
        <div className="flex justify-center mb-7">
          <div className="w-14 h-14 rounded-2xl bg-accent/10 border border-accent/20 flex items-center justify-center">
            <TrinityMark className="w-7 h-6 text-accent-light" />
          </div>
        </div>

        <div className="text-center mb-9">
          <h1 className="text-[1.75rem] font-semibold text-text-primary tracking-tight mb-2.5">
            O que você quer fazer?
          </h1>
          <p className="text-sm text-text-muted leading-relaxed">
            Diga com suas palavras. O TRINDATRON decide como executar e mostra cada passo.
          </p>
        </div>

        <Composer
          onSend={handleSend}
          disabled={creating}
          placeholder="Escreva o que precisa fazer…"
          ensureSession={ensureSession}
        />

        {agents.length > 0 && (
          <div className="mt-3 flex items-center gap-2 flex-wrap" data-testid="agent-selector">
            <span className="text-[10px] font-medium text-text-muted uppercase tracking-wider shrink-0">
              Agente:
            </span>
            {agents.map((agent) => {
              const selected = selectedAgentId === agent.id
              return (
                <button
                  key={agent.id}
                  type="button"
                  onClick={() => setSelectedAgentId(selected ? null : agent.id)}
                  className={[
                    'flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs transition-all border',
                    selected
                      ? 'bg-accent/15 border-accent/40 text-accent-light'
                      : 'bg-surface-1 border-border text-text-secondary hover:text-text-primary hover:bg-surface-2',
                  ].join(' ')}
                  aria-pressed={selected}
                >
                  {agent.avatar_emoji
                    ? <span className="text-[13px] leading-none">{agent.avatar_emoji}</span>
                    : <Bot className="w-3 h-3" />
                  }
                  <span>{agent.name}</span>
                  {selected && <X className="w-2.5 h-2.5 ml-0.5 opacity-70" />}
                </button>
              )
            })}
          </div>
        )}

        {usable.length > 0 && (
          <div className="mt-7 grid grid-cols-1 sm:grid-cols-2 gap-2" data-testid="home-actions">
            {usable.map((action) => {
              const Icon = action.icon
              return (
                <button
                  key={action.id}
                  type="button"
                  onClick={() => start(action)}
                  disabled={creating}
                  className="group text-left px-4 py-3 rounded-xl bg-surface-1 border border-border hover:bg-surface-2 hover:border-accent/30 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  <div className="flex items-center gap-2 mb-1">
                    <Icon className="w-3.5 h-3.5 text-text-muted group-hover:text-accent-light transition-colors shrink-0" />
                    <span className="text-xs font-medium text-text-primary">{action.label}</span>
                  </div>
                  <p className="text-[11px] text-text-muted leading-relaxed">{action.hint}</p>
                </button>
              )
            })}
          </div>
        )}

        {gated.length > 0 && (
          <div className="mt-6 pt-5 border-t border-border">
            <ul className="flex flex-wrap gap-x-4 gap-y-1.5">
              {gated.map((action) => (
                <li key={action.id} className="text-[11px] text-text-muted">
                  {action.label} — {STATUS_NOTE[known(action)?.status ?? ''] ?? 'Indisponível agora'}
                  {known(action)?.status === 'plan' && (
                    <Link href="/plans" className="ml-1 text-accent-light hover:underline">ver planos</Link>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  )
}
