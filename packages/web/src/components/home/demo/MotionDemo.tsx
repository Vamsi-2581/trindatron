'use client'

import { useEffect, useRef, useState } from 'react'
import { Play } from 'lucide-react'
import { useLocale } from '../i18n'
import s from '../home.module.css'

/**
 * A still photograph that comes alive: the frame rests as a photo, is read,
 * then moves, and settles back into a photo before it starts again. It runs
 * only while it is on screen; with reduced motion it waits for a tap.
 */
export function MotionDemo() {
  const { t } = useLocale()
  const m = t.studio.motion
  const video = useRef<HTMLVideoElement>(null)
  const root = useRef<HTMLElement>(null)
  const [state, setState] = useState<'photo' | 'motion'>('photo')
  const [reduced, setReduced] = useState(false)
  const timer = useRef(0)

  useEffect(() => {
    const v = video.current
    const el = root.current
    if (!v || !el) return
    const isReduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    setReduced(isReduced)
    const start = () => {
      window.clearTimeout(timer.current)
      timer.current = window.setTimeout(() => {
        v.currentTime = 0
        v.play().then(() => setState('motion')).catch(() => {})
      }, 1500)
    }
    const onEnded = () => {
      timer.current = window.setTimeout(() => { setState('photo'); start() }, 1200)
    }
    v.addEventListener('ended', onEnded)
    const io = new IntersectionObserver((entries) => {
      const on = entries.some((e) => e.isIntersecting)
      window.clearTimeout(timer.current)
      if (on && !isReduced) { setState('photo'); start() }
      else { v.pause(); setState('photo') }
    }, { threshold: 0.4 })
    io.observe(el)
    return () => { io.disconnect(); v.removeEventListener('ended', onEnded); window.clearTimeout(timer.current) }
  }, [])

  const playOnce = () => {
    const v = video.current
    if (!v) return
    v.currentTime = 0
    v.play().then(() => setState('motion')).catch(() => {})
  }

  return (
    <figure ref={root} className={s.motion} data-state={state} data-testid="motion-demo">
      <video ref={video} muted playsInline preload="none" poster="/home/media/motion-still.jpg" aria-label={m.alt}>
        <source src="/home/media/motion.mp4" type="video/mp4" />
      </video>
      {/* eslint-disable-next-line @next/next/no-img-element -- the photograph itself, a fixed file from public/ */}
      <img className={s.motionStill} src="/home/media/motion-still.jpg" alt="" loading="lazy" decoding="async" />
      <span className={s.motionScan} aria-hidden="true" />
      <div className={s.motionTags} aria-hidden="true">
        <span data-on={state === 'photo'}>{m.photo}</span>
        <i />
        <span data-on={state === 'motion'}>{m.moving}</span>
      </div>
      {reduced && state === 'photo' && (
        <button type="button" className={s.filmPlay} onClick={playOnce}>
          <Play size={16} aria-hidden="true" /> {t.replay.play}
        </button>
      )}
    </figure>
  )
}
