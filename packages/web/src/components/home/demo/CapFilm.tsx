'use client'

import { useRef } from 'react'
import { Play } from 'lucide-react'
import { useLocale } from '../i18n'
import { useAutoplay } from './useAutoplay'
import s from '../home.module.css'

/**
 * A capability shown as a short film (public/home/caps): the request, the work
 * and the finished result, rendered in the page's language. It loops while it
 * is on screen and rests on its last frame otherwise.
 */
export function CapFilm({ cap, label }: { cap: 'slides' | 'docs' | 'computer' | 'code'; label: string }) {
  const { lang, t } = useLocale()
  const video = useRef<HTMLVideoElement>(null)
  const src = `/home/caps/cap-${cap}-${lang}`
  const { reduced } = useAutoplay(video, 0.35, src)

  return (
    <figure className={s.capFilm} data-testid={`capfilm-${cap}`}>
      <video
        key={src}
        ref={video}
        muted
        loop
        playsInline
        preload="none"
        poster={`${src}-poster.jpg`}
        aria-label={label}
      >
        <source src={`${src}.mp4`} type="video/mp4" />
      </video>
      {reduced && (
        <button type="button" className={s.filmPlay} onClick={() => video.current?.play().catch(() => {})}>
          <Play size={16} aria-hidden="true" /> {t.replay.play}
        </button>
      )}
    </figure>
  )
}
