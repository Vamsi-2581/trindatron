'use client'

import { useEffect, useRef, useState } from 'react'
import { RotateCcw } from 'lucide-react'
import { useLocale } from '../i18n'
import { useStage } from '../stage/Stage'
import { CINEMA, gsap, ScrollTrigger } from '../motion'
import s from '../home.module.css'

/**
 * The big demonstration: a film made from one real TRINDATRON session
 * (public/home/film, recorded from the product; every word, step and page on
 * screen is the session's own, only time is compressed).
 *
 * It is not an embedded player. On wide screens the film opens from a window in
 * the page to the whole viewport as it arrives, its edges dissolving into the
 * stage; it plays while it is on screen and ends on the page's own dark. Nothing
 * holds the scroll: the page moves under the wheel exactly as it would anywhere.
 */
export function Film() {
  const { t, lang } = useLocale()
  const { pulse } = useStage()
  const root = useRef<HTMLElement>(null)
  const frame = useRef<HTMLDivElement>(null)
  const video = useRef<HTMLVideoElement>(null)
  const [portrait, setPortrait] = useState(false)
  const [ended, setEnded] = useState(false)

  useEffect(() => {
    const mq = window.matchMedia('(max-aspect-ratio: 9/10)')
    const on = () => setPortrait(mq.matches)
    on()
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])

  const src = `/home/film/trindatron-demo-${lang}-${portrait ? 'port' : 'land'}`

  // Plays when it has the stage; pauses when it leaves. With reduced motion it waits for a tap.
  useEffect(() => {
    const el = root.current
    const v = video.current
    if (!el || !v) return
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduced) return
    const st = ScrollTrigger.create({
      trigger: el,
      start: 'top 35%',
      end: 'bottom 40%',
      onToggle: (self) => {
        if (self.isActive && !ended) { v.play().catch(() => {}); pulse() }
        else v.pause()
      },
    })
    return () => st.kill()
  }, [src, ended, pulse])

  // Opening: a window in the page that grows to the whole viewport.
  useEffect(() => {
    const el = root.current
    const f = frame.current
    if (!el || !f) return
    const mm = gsap.matchMedia()
    mm.add(CINEMA, () => {
      gsap.fromTo(f, { '--inset': '14%', '--radius': '36px' }, {
        '--inset': '0%', '--radius': '0px', ease: 'none',
        scrollTrigger: { trigger: el, start: 'top bottom', end: 'top top', scrub: 0.4 },
      })
    })
    return () => mm.revert()
  }, [])

  const replay = () => {
    const v = video.current
    if (!v) return
    setEnded(false)
    v.currentTime = 0
    v.play().catch(() => {})
    pulse()
  }

  return (
    <section ref={root} id="film" className={s.film} data-chapter="film" aria-labelledby="film-title">
      <h2 id="film-title" className={s.srOnly}>{t.film.title}</h2>
      <div ref={frame} className={s.filmFrame}>
        <div className={`${s.filmBox} ${portrait ? s.filmBoxPort : ''}`}>
          <video
            key={src}
            ref={video}
            className={s.filmVideo}
            muted
            playsInline
            preload="metadata"
            poster={`${src}-poster.jpg`}
            onEnded={() => setEnded(true)}
            aria-label={t.film.alt}
          >
            <source src={`${src}.mp4`} type="video/mp4" />
          </video>
        </div>
      </div>
      <div className={s.filmMeta}>
        <span>{t.film.caption}</span>
        <button type="button" className={s.textButton} onClick={replay}>
          <RotateCcw size={14} aria-hidden="true" /> {ended ? t.replay.replay : t.replay.play}
        </button>
      </div>
    </section>
  )
}
