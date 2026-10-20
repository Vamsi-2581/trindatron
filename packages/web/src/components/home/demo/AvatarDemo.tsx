'use client'

import { useRef, useState } from 'react'
import { Volume2, VolumeX } from 'lucide-react'
import { useLocale } from '../i18n'
import { useAutoplay } from './useAutoplay'
import s from '../home.module.css'

/**
 * A presenter created by TRINDATRON (public/home/media/avatar-*), saying one
 * line in the page's language. It plays silently with the line as a subtitle;
 * one tap turns the sound on and starts her from the beginning.
 */
export function AvatarDemo() {
  const { lang, t } = useLocale()
  const a = t.studio.avatar
  const video = useRef<HTMLVideoElement>(null)
  const [sound, setSound] = useState(false)
  const src = `/home/media/avatar-${lang}`
  useAutoplay(video, 0.4, src)

  const toggle = () => {
    const v = video.current
    if (!v) return
    const next = !sound
    v.muted = !next
    if (next) { v.currentTime = 0; v.play().catch(() => {}) }
    setSound(next)
  }

  return (
    <figure className={s.avatar} data-testid="avatar-demo">
      <div className={s.avatarFrame}>
        <video key={src} ref={video} muted={!sound} loop playsInline preload="none" poster={`${src}-poster.jpg`} aria-label={a.alt}>
          <source src={`${src}.mp4`} type="video/mp4" />
        </video>
        <p className={s.avatarLine}>{a.line}</p>
        <button type="button" className={s.avatarSound} onClick={toggle} aria-pressed={sound}>
          {sound ? <VolumeX size={16} aria-hidden="true" /> : <Volume2 size={16} aria-hidden="true" />}
          {sound ? a.mute : a.listen}
        </button>
      </div>
      <figcaption className={s.replayCaption}><span>{t.studio.made} · {a.date}</span></figcaption>
    </figure>
  )
}
