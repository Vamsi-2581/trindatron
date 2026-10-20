'use client'

import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { ArrowRight, Check } from 'lucide-react'
import {
  apiApproveAgentAction,
  apiCreateSession,
  apiRejectAgentAction,
  apiSendMessage,
  bootstrapSession,
  hasSessionHint,
} from '@/api/client'
import type { ApprovalRequiredEvent } from '@/types'
import { BrandSymbol } from '@/components/brand/master'
import { activityLabel, useLocale } from '../i18n'
import { AuthGate, type AuthMode } from './AuthGate'
import { readPending, savePending, track } from './funnel'
import s from '../home.module.css'

type Activity = { key: string; done: boolean }
type Turn =
  | { id: string; role: 'user'; text: string }
  | { id: string; role: 'assistant'; text: string; state: 'streaming' | 'done' | 'error'; steps: Activity[]; approval?: ApprovalRequiredEvent & { resolved?: 'approved' | 'rejected' } }
  | { id: string; role: 'gate'; mode: AuthMode }
  | { id: string; role: 'note'; text: string }

export type Auth = 'checking' | 'anon' | 'signed'

export interface ChatHandle {
  /** Put text in the composer and focus it. */
  prefill: (text: string) => void
  /** Send a message as if typed here (the hero's input uses this). */
  submit: (text: string) => void
  focus: () => void
  openGate: (mode: AuthMode) => void
}

const uid = () => Math.random().toString(36).slice(2, 10)

/**
 * The real product, on the Home. Messages go to POST /sessions/{id}/message,
 * the same endpoint the app uses, which routes to conversation or to the
 * agent (search, browser) on its own. Visitors sign up before the first
 * answer; the message waits and is sent the moment they are in.
 */
export const Chat = forwardRef<ChatHandle, { onAuthChange?: (auth: Auth) => void }>(function Chat({ onAuthChange }, ref) {
  const { t, lang } = useLocale()
  const c = t.chat
  const [turns, setTurns] = useState<Turn[]>([])
  const [draft, setDraft] = useState('')
  const [auth, setAuth] = useState<Auth>('checking')
  const [busy, setBusy] = useState(false)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const pending = useRef<string | null>(null)
  const sentFirst = useRef(false)
  const repliedFirst = useRef(false)
  const composer = useRef<HTMLTextAreaElement>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const authRef = useRef<Auth>('checking')
  authRef.current = auth

  const gated = turns.some((turn) => turn.role === 'gate')

  useEffect(() => { onAuthChange?.(auth) }, [auth, onAuthChange])

  // A returning visitor never meets the gate; a message left behind the gate survives a reload.
  useEffect(() => {
    let alive = true
    ;(async () => {
      const signed = hasSessionHint() ? await bootstrapSession() : false
      if (!alive) return
      setAuth(signed ? 'signed' : 'anon')
      const waiting = readPending()
      if (waiting && !signed) {
        pending.current = waiting
        setTurns([{ id: uid(), role: 'user', text: waiting }, { id: uid(), role: 'gate', mode: 'signup' }])
      }
    })()
    return () => { alive = false }
  }, [])

  // Follow the conversation; when the sign-up step appears, show it from its first line.
  useEffect(() => {
    const el = scroller.current
    if (!el || turns.length === 0) return
    const last = turns[turns.length - 1]
    const node = el.querySelector<HTMLElement>(`[data-turn="${last.id}"]`)
    if (last.role === 'gate' && node) el.scrollTop = Math.max(0, node.offsetTop - 16)
    else el.scrollTop = el.scrollHeight
  }, [turns])

  const patchAssistant = (id: string, fn: (turn: Extract<Turn, { role: 'assistant' }>) => Extract<Turn, { role: 'assistant' }>) =>
    setTurns((all) => all.map((turn) => (turn.id === id && turn.role === 'assistant' ? fn(turn) : turn)))

  const stream = useCallback(async (text: string) => {
    setBusy(true)
    let sid = sessionId
    if (!sid) {
      try {
        const created = await apiCreateSession(text.slice(0, 60))
        sid = created.id
        setSessionId(sid)
      } catch {
        setTurns((all) => [...all, { id: uid(), role: 'assistant', text: c.openFailed, state: 'error', steps: [] }])
        setBusy(false)
        return
      }
    }
    const id = uid()
    setTurns((all) => [...all, { id, role: 'assistant', text: '', state: 'streaming', steps: [] }])
    const addStep = (key: string, done: boolean) =>
      patchAssistant(id, (turn) => {
        const steps = [...turn.steps]
        const open = steps.length ? steps[steps.length - 1] : null
        if (open && open.key === key) { if (done) open.done = true; return { ...turn, steps } }
        if (open) open.done = true
        steps.push({ key, done })
        return { ...turn, steps }
      })

    await apiSendMessage(
      sid!,
      text,
      (chunk) => patchAssistant(id, (turn) => ({ ...turn, text: turn.text + chunk })),
      () => {
        patchAssistant(id, (turn) => ({ ...turn, state: 'done', steps: turn.steps.map((st) => ({ ...st, done: true })) }))
        if (!repliedFirst.current) { repliedFirst.current = true; track({ name: 'first_reply' }) }
      },
      (message) => patchAssistant(id, (turn) => ({ ...turn, text: turn.text || message, state: 'error' })),
      (key) => { if (key.startsWith('activity.')) addStep(key, false) },
      (step) => addStep(step.activity, step.type === 'tool_result'),
      (event) => patchAssistant(id, (turn) => ({ ...turn, approval: event })),
    )
    setBusy(false)
  }, [sessionId, c.openFailed])

  const send = useCallback((raw: string) => {
    const text = raw.trim()
    if (!text || busy || authRef.current === 'checking') return false
    if (turns.some((turn) => turn.role === 'gate')) return false
    setTurns((all) => [...all, { id: uid(), role: 'user', text }])
    if (!sentFirst.current) { sentFirst.current = true; track({ name: 'first_message' }) }
    if (authRef.current !== 'signed') {
      pending.current = text
      savePending(text)
      setTurns((all) => [...all, { id: uid(), role: 'gate', mode: 'signup' }])
      track({ name: 'gate_shown' })
      return true
    }
    void stream(text)
    return true
  }, [busy, turns, stream])

  useImperativeHandle(ref, () => ({
    prefill: (text) => { setDraft(text); requestAnimationFrame(() => composer.current?.focus({ preventScroll: true })) },
    submit: (text) => { if (!send(text)) setDraft(text) },
    focus: () => composer.current?.focus({ preventScroll: true }),
    openGate: (mode) => {
      if (authRef.current === 'signed') return
      setTurns((all) => [...all.filter((turn) => turn.role !== 'gate'), { id: uid(), role: 'gate', mode }])
      track({ name: 'gate_shown' })
    },
  }), [send])

  function onAuthed(how: AuthMode) {
    track({ name: how === 'signup' ? 'signup_completed' : 'login_completed' })
    setAuth('signed')
    const text = pending.current
    pending.current = null
    savePending(null)
    setTurns((all) => [...all.filter((turn) => turn.role !== 'gate'), { id: uid(), role: 'note', text: how === 'signup' ? c.created : c.signedIn }])
    if (text) void stream(text)
    requestAnimationFrame(() => composer.current?.focus({ preventScroll: true }))
  }

  async function resolve(turnId: string, approval: ApprovalRequiredEvent, approve: boolean) {
    if (!sessionId) return
    try {
      if (approve) await apiApproveAgentAction(sessionId, approval.action_id)
      else await apiRejectAgentAction(sessionId, approval.action_id)
      patchAssistant(turnId, (turn) => ({ ...turn, approval: turn.approval && { ...turn.approval, resolved: approve ? 'approved' : 'rejected' } }))
    } catch {
      // The stream reports what happened next; the buttons stay for another try.
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      if (send(draft)) setDraft('')
    }
  }

  const empty = turns.length === 0
  const canSend = !!draft.trim() && !busy && !gated && auth !== 'checking'

  return (
    <section aria-label={c.title} className={s.chat} data-no-stage>
      <header className={s.chatHead}>
        <span className={s.chatHeadName}><BrandSymbol size={22} /> {c.newChat}</span>
        {sessionId && auth === 'signed' ? (
          <a href={`/chat/${sessionId}`} className={s.textLink}>{c.openWorkspace}</a>
        ) : (
          <span className={s.mono}>{c.enter}</span>
        )}
      </header>

      <div ref={scroller} className={s.chatBody} aria-live="polite">
        {empty ? (
          <div className={s.chatEmpty}>
          <BrandSymbol size={84} />
          <p className={s.chatEmptyTitle}>{c.empty}</p>
          <ul className={s.starters} aria-label={c.kicker}>
            {c.starters.map((text) => (
              <li key={text}>
                <button type="button" onClick={() => { setDraft(text); requestAnimationFrame(() => composer.current?.focus()) }}>
                  <span>{text}</span>
                  <ArrowRight size={15} aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
          </div>
        ) : (
          <ol className={s.turns}>
            {turns.map((turn) => (
              <li key={turn.id} className={s.turn} data-turn={turn.id}>
                {turn.role === 'user' && <p className={s.bubbleUser}>{turn.text}</p>}

                {turn.role === 'assistant' && (
                  <div className={s.bubbleAi}>
                    <BrandSymbol size={26} className={s.bubbleAvatar} />
                    <div className={s.bubbleAiBody} data-testid="assistant-turn">
                      {turn.steps.length > 0 && (
                        <ol className={s.replaySteps}>
                          {turn.steps.map((st, i) => (
                            <li key={i} data-done={st.done || turn.state !== 'streaming'}>
                              {st.done || turn.state !== 'streaming' ? <Check size={14} aria-hidden="true" /> : <span className={s.replaySpin} aria-hidden="true" />}
                              <span>{activityLabel(st.key, lang)}</span>
                            </li>
                          ))}
                        </ol>
                      )}
                      {turn.approval && (
                        <div className={s.approvalCard} role="group" aria-label={c.wantsTo}>
                          <p className={s.approvalWants}>{c.wantsTo}</p>
                          <p className={s.approvalAction}>{turn.approval.description || turn.approval.label}</p>
                          {turn.approval.resolved ? (
                            <p className={s.approvalDone}>{turn.approval.resolved === 'approved' ? t.approvalDemo.approved : t.approvalDemo.rejected}</p>
                          ) : (
                            <div className={s.approvalButtons}>
                              <button type="button" className={s.btnPrimary} onClick={() => resolve(turn.id, turn.approval!, true)}>{c.approve}</button>
                              <button type="button" className={s.btnGhost} onClick={() => resolve(turn.id, turn.approval!, false)}>{c.reject}</button>
                            </div>
                          )}
                        </div>
                      )}
                      {turn.text ? (
                        <div className={`${s.md} ${turn.state === 'error' ? s.mdError : ''}`}>
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>{turn.text}</ReactMarkdown>
                        </div>
                      ) : turn.steps.length === 0 ? (
                        <p className={s.thinking}>{c.thinking}<span className={s.caret} aria-hidden="true" /></p>
                      ) : null}
                    </div>
                  </div>
                )}

                {turn.role === 'gate' && (
                  <div className={s.bubbleAi}>
                    <BrandSymbol size={26} className={s.bubbleAvatar} />
                    <div className={s.bubbleAiBody}>
                      <p className={s.gateLead}>{pending.current ? c.gateNeed : turn.mode === 'login' ? c.gateLogin : c.gateSignup}</p>
                      <p className={s.gateWhy}>{c.gateWhy}</p>
                      <AuthGate onAuthed={onAuthed} initialMode={turn.mode} />
                    </div>
                  </div>
                )}

                {turn.role === 'note' && <p className={s.note}>{turn.text}</p>}
              </li>
            ))}
          </ol>
        )}
      </div>

      <form onSubmit={(e) => { e.preventDefault(); if (send(draft)) setDraft('') }} className={s.composer}>
        <textarea
          ref={composer}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          rows={1}
          aria-label={c.placeholder}
          placeholder={gated ? c.locked : c.placeholder}
          disabled={gated}
        />
        <button type="submit" aria-label={c.send} disabled={!canSend} className={s.sendButton}>
          <BrandSymbol size={30} />
        </button>
      </form>
    </section>
  )
})
