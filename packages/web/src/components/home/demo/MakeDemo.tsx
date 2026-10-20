'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { Check, RotateCcw } from 'lucide-react'
import { BrandSymbol } from '@/components/brand/master'
import { useLocale } from '../i18n'
import s from '../home.module.css'

/**
 * Replays one real creation (public/home/media/media.json): the prompt exactly
 * as it was sent, the wait, then the file the product returned. Nothing on
 * screen is drawn for the demo except the time-lapse of the wait itself.
 */

interface Made { prompt: string; seconds: number; src: string; poster?: string }
interface Manifest { recordedAt: string; langs: Record<string, { image: Made; video: Made }> }

let manifest: Promise<Manifest | null> | null = null
const loadManifest = () => (manifest ??= fetch('/home/media/media.json').then((r) => (r.ok ? r.json() : null)).catch(() => null))

type State = 'idle' | 'typing' | 'making' | 'done'

const TYPE_MS = 22
const MAKING_MS = { image: 2200, video: 3200 }

export function MakeDemo({ kind }: { kind: 'image' | 'video' }) {
  const { lang, t } = useLocale()
  const c = t.create
  const [made, setMade] = useState<Made | null>(null)
  const [date, setDate] = useState('')
  const [state, setState] = useState<State>('idle')
  const [typed, setTyped] = useState(0)
  const root = useRef<HTMLElement>(null)
  const video = useRef<HTMLVideoElement>(null)
  const timers = useRef<number[]>([])
  const played = useRef(false)

  useEffect(() => {
    let alive = true
    loadManifest().then((m) => {
      if (!alive || !m) return
      setMade(m.langs[lang]?.[kind] ?? null)
      setDate(new Intl.DateTimeFormat(t.meta.locale, { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(`${m.recordedAt}T12:00:00`)))
    })
    return () => { alive = false }
  }, [lang, kind, t.meta.locale])

  const clear = () => { timers.current.forEach((id) => window.clearTimeout(id)); timers.current = [] }

  const play = useCallback(() => {
    if (!made) return
    clear()
    setTyped(0)
    setState('typing')
    const step = Math.max(1, Math.round(made.prompt.length / 60))
    let at = 300
    for (let i = step; i < made.prompt.length + step; i += step) {
      const n = Math.min(i, made.prompt.length)
      timers.current.push(window.setTimeout(() => setTyped(n), at))
      at += TYPE_MS * step
    }
    timers.current.push(window.setTimeout(() => setState('making'), at + 300))
    timers.current.push(window.setTimeout(() => setState('done'), at + 300 + MAKING_MS[kind]))
  }, [made, kind])

  // Finished until it is seen, then it plays once; reduced motion keeps it finished.
  useEffect(() => {
    if (!made || !root.current) return
    played.current = false
    setTyped(made.prompt.length)
    setState('idle')
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) { setState('done'); return }
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting) && !played.current) { played.current = true; play() }
    }, { threshold: 0.4 })
    io.observe(root.current)
    return () => { io.disconnect(); clear() }
  }, [made, play])

  // The clip runs only while it is on screen and revealed.
  useEffect(() => {
    const v = video.current
    if (!v || !root.current) return
    const io = new IntersectionObserver((entries) => {
      const visible = entries.some((e) => e.isIntersecting)
      if (visible && (state === 'done' || state === 'idle')) v.play().catch(() => {})
      else v.pause()
    }, { threshold: 0.25 })
    io.observe(root.current)
    if (state === 'making' || state === 'typing') { v.pause(); v.currentTime = 0 }
    return () => io.disconnect()
  }, [state, made])

  const revealed = state === 'done' || state === 'idle'
  const label = kind === 'image' ? c.image : c.video

  return (
    <figure ref={root} className={s.make} data-kind={kind} data-state={state} data-testid={`make-${kind}`}>
      <div className={s.makeCanvas} data-revealed={revealed}>
        {made && (kind === 'image' ? (
          // eslint-disable-next-line @next/next/no-img-element -- a fixed, pre-sized file from public/
          <img src={made.src} alt={label.alt} className={s.makeMedia} loading="lazy" decoding="async" />
        ) : (
          <video
            ref={video}
            key={made.src}
            className={s.makeMedia}
            muted
            loop
            playsInline
            preload="metadata"
            poster={made.poster}
            aria-label={label.alt}
          >
            <source src={made.src} type="video/mp4" />
          </video>
        ))}
        <div className={s.makeVeil} aria-hidden="true" />
        <div className={s.makeStatus} aria-live="polite">
          {state === 'making' ? <span className={s.replaySpin} aria-hidden="true" /> : state === 'typing' ? null : <Check size={14} aria-hidden="true" />}
          <span>
            {state === 'making' ? label.making : state === 'typing' ? '' : label.ready.replace('{s}', String(made?.seconds ?? ''))}
          </span>
          {state === 'making' && <i className={s.makeBar} style={{ animationDuration: `${MAKING_MS[kind]}ms` }} />}
        </div>
      </div>

      <div className={s.makeAsk}>
        <BrandSymbol size={22} />
        <p>
          {made?.prompt.slice(0, typed)}
          {state === 'typing' && <span className={s.caret} aria-hidden="true" />}
        </p>
      </div>

      <figcaption className={s.replayCaption}>
        <span>{c.real} · {c.created} {date} · {c.timelapse}</span>
        <button type="button" onClick={play} className={s.textButton} disabled={!made}>
          <RotateCcw size={14} aria-hidden="true" /> {state === 'idle' ? t.replay.play : t.replay.replay}
        </button>
      </figcaption>
    </figure>
  )
}
