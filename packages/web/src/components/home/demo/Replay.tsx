'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Check, FileText, Globe, RotateCcw, Sparkles } from 'lucide-react'
import { BrandSymbol } from '@/components/brand/master'
import { activityLabel, useLocale } from '../i18n'
import s from '../home.module.css'

/**
 * Replays a session recorded from the real product (public/home/sessions/*.json,
 * captured from the TRINDATRON API and never edited). The order of events is
 * the product's; waiting times are shortened and labelled "time-lapse".
 */

interface Step { t: number; kind: 'status' | 'call' | 'result'; activity?: string; ok?: boolean; url?: string }
interface Session {
  id: string
  demo: string
  lang: 'en' | 'pt' | 'es'
  recordedAt: string
  prompt: string
  attachment?: { name: string }
  /** The photo that was attached, for a vision session. */
  image?: string
  memory?: string
  steps: Step[]
  text: { t: number; c: string }[]
}

interface Row { activity: string; done: boolean; url?: string }

type Beat =
  | { at: number; kind: 'type'; chars: number }
  | { at: number; kind: 'rows'; rows: Row[] }
  | { at: number; kind: 'answer'; chars: number }

const TYPE_MS = 26
const ANSWER_CPS = 900

/** Turns the recorded events into a timeline of what the viewer sees. */
function choreograph(session: Session): { beats: Beat[]; answer: string; end: number } {
  const beats: Beat[] = []
  let at = 400
  const promptLen = session.prompt.length
  const typeStep = Math.max(1, Math.round(promptLen / 70))
  for (let i = typeStep; i < promptLen + typeStep; i += typeStep) {
    beats.push({ at, kind: 'type', chars: Math.min(i, promptLen) })
    at += TYPE_MS * typeStep
  }
  at += 500

  const rows: Row[] = []
  let lastT = 0
  for (const step of session.steps) {
    const gap = Math.max(260, Math.min(1300, (step.t - lastT) * 0.35))
    lastT = step.t
    const key = step.activity ?? ''
    const open = rows.length ? rows[rows.length - 1] : null
    if (step.kind === 'result') {
      if (open && open.activity === key) {
        open.done = true
        if (step.url) open.url = step.url
      } else {
        rows.push({ activity: key, done: true, url: step.url })
      }
    } else if (!open || open.activity !== key || open.done) {
      if (open && !open.done) open.done = true
      rows.push({ activity: key, done: false })
    } else {
      continue
    }
    at += gap
    beats.push({ at, kind: 'rows', rows: rows.map((r) => ({ ...r })) })
  }
  if (rows.some((r) => !r.done)) {
    at += 400
    beats.push({ at, kind: 'rows', rows: rows.map((r) => ({ ...r, done: true })) })
  }

  const answer = session.text.map((x) => x.c).join('')
  at += 500
  const chunk = Math.max(8, Math.round(ANSWER_CPS / 20))
  for (let i = chunk; i < answer.length + chunk; i += chunk) {
    beats.push({ at, kind: 'answer', chars: Math.min(i, answer.length) })
    at += 50
  }
  return { beats, answer, end: at }
}

function shortUrl(url: string): string {
  try {
    const u = new URL(url)
    const path = decodeURIComponent(u.pathname).replace(/\/$/, '')
    return `${u.hostname.replace(/^www\./, '')}${path.length > 28 ? `${path.slice(0, 28)}…` : path}`
  } catch {
    return url
  }
}

export function Replay({ demo, className = '' }: { demo: 'web' | 'memory' | 'files' | 'vision'; className?: string }) {
  const { lang, t } = useLocale()
  const [session, setSession] = useState<Session | null>(null)
  const [typed, setTyped] = useState(0)
  const [rows, setRows] = useState<Row[]>([])
  const [shown, setShown] = useState(0)
  const [state, setState] = useState<'idle' | 'playing' | 'done'>('idle')
  const root = useRef<HTMLElement>(null)
  const timers = useRef<number[]>([])
  const played = useRef(false)

  useEffect(() => {
    let alive = true
    setSession(null)
    fetch(`/home/sessions/${demo}-${lang}.json`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data: Session | null) => { if (alive) setSession(data) })
      .catch(() => {})
    return () => { alive = false }
  }, [demo, lang])

  const plan = useMemo(() => (session ? choreograph(session) : null), [session])

  const clear = () => { timers.current.forEach((id) => window.clearTimeout(id)); timers.current = [] }

  const showFinal = useCallback(() => {
    if (!session || !plan) return
    clear()
    setTyped(session.prompt.length)
    const last = [...plan.beats].reverse().find((b) => b.kind === 'rows') as Extract<Beat, { kind: 'rows' }> | undefined
    setRows(last?.rows ?? [])
    setShown(plan.answer.length)
    setState('done')
  }, [session, plan])

  const play = useCallback(() => {
    if (!session || !plan) return
    clear()
    setTyped(0); setRows([]); setShown(0); setState('playing')
    for (const beat of plan.beats) {
      timers.current.push(window.setTimeout(() => {
        if (beat.kind === 'type') setTyped(beat.chars)
        else if (beat.kind === 'rows') setRows(beat.rows)
        else setShown(beat.chars)
      }, beat.at))
    }
    timers.current.push(window.setTimeout(() => setState('done'), plan.end + 100))
  }, [session, plan])

  // Until it plays, the finished session is shown (never an empty window). It plays once when it
  // comes into view; with reduced motion it simply stays finished.
  useEffect(() => {
    if (!plan || !root.current) return
    played.current = false
    showFinal()
    setState('idle')
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduced) { setState('done'); return }
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting) && !played.current) {
        played.current = true
        play()
      }
    }, { threshold: 0.45 })
    io.observe(root.current)
    return () => { io.disconnect(); clear() }
  }, [plan, play, showFinal])

  const finalRows = useMemo(() => {
    const last = plan ? ([...plan.beats].reverse().find((b) => b.kind === 'rows') as Extract<Beat, { kind: 'rows' }> | undefined) : undefined
    return last?.rows ?? []
  }, [plan])

  const content = (typedN: number, stepRows: Row[], shownN: number, live: boolean) => session && plan && (
    <>
      {session.memory && (
        <div className={s.replayMemory}>
          <Sparkles size={14} aria-hidden="true" />
          <span>{t.replay.remembered}</span>
          <q>{session.memory}</q>
        </div>
      )}
      {session.memory && <div className={s.replayDivider}><span>{t.replay.newConversation}</span></div>}
      {session.attachment && session.image && (
        <figure className={s.replayPhoto}>
          {/* eslint-disable-next-line @next/next/no-img-element -- a fixed, pre-sized file from public/ */}
          <img src={session.image} alt="" loading="lazy" decoding="async" />
          <figcaption>{session.attachment.name} · {t.replay.attached}</figcaption>
        </figure>
      )}
      {session.attachment && !session.image && (
        <div className={s.replayFile}>
          <FileText size={18} aria-hidden="true" />
          <span>
            <strong>{session.attachment.name}</strong>
            <small>{t.replay.attached} · {t.replay.sample}</small>
          </span>
        </div>
      )}
      <p className={s.replayUser}>
        {session.prompt.slice(0, typedN)}
        {live && typedN < session.prompt.length && <span className={s.caret} aria-hidden="true" />}
      </p>
      {stepRows.length > 0 && (
        <ol className={s.replaySteps}>
          {stepRows.map((row, i) => (
            <li key={i} data-done={row.done}>
              {row.done ? <Check size={14} aria-hidden="true" /> : <span className={s.replaySpin} aria-hidden="true" />}
              <span>{activityLabel(row.activity, lang)}</span>
              {row.url && (
                <span className={s.replayUrl}>
                  <Globe size={12} aria-hidden="true" /> {t.replay.opened} {shortUrl(row.url)}
                </span>
              )}
            </li>
          ))}
        </ol>
      )}
      {shownN > 0 && (
        <div className={s.replayAnswer}>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{plan.answer.slice(0, shownN)}</ReactMarkdown>
        </div>
      )}
    </>
  )

  const date = session
    ? new Intl.DateTimeFormat(t.meta.locale, { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(`${session.recordedAt}T12:00:00`))
    : ''

  return (
    <figure ref={root} className={`${s.replay} ${className}`} data-state={state} data-testid={`replay-${demo}`}>
      <div className={s.replayWindow}>
        <div className={s.replayBar}>
          <BrandSymbol size={22} />
          <span className={s.replayName}>TRINDATRON</span>
          <span className={s.replayLive}>{t.replay.real}</span>
        </div>
        {/* No inner scroll: a wheel over the window always moves the page. The finished session sits
            invisibly underneath and gives the window its height, so nothing jumps while it plays. */}
        <div className={s.replayBody}>
          {!session || !plan ? (
            <div className={s.replaySkeleton} aria-hidden="true"><span /><span /><span /></div>
          ) : (
            <div className={s.replayStack}>
              <div className={s.replayGhost} aria-hidden="true">
                {content(session.prompt.length, finalRows, plan.answer.length, false)}
              </div>
              <div>{content(typed, rows, shown, state === 'playing')}</div>
            </div>
          )}
        </div>
      </div>
      <figcaption className={s.replayCaption}>
        <span>
          {t.replay.real} · {t.replay.recorded} {date} · {t.replay.timelapse}
          {session && session.lang !== lang && t.replay.recordedIn ? ` · ${t.replay.recordedIn}` : ''}
        </span>
        <button type="button" onClick={play} className={s.textButton} disabled={!session}>
          <RotateCcw size={14} aria-hidden="true" /> {state === 'idle' ? t.replay.play : t.replay.replay}
        </button>
      </figcaption>
    </figure>
  )
}
