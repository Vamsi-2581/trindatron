'use client'

import { useEffect, useRef, useState } from 'react'
import { Play } from 'lucide-react'
import { useLocale } from '../i18n'
import { useAutoplay } from './useAutoplay'
import s from '../home.module.css'

/**
 * A finished edit next to the timeline it was cut from. The reel
 * (public/home/media/edit) is cut from the clips and portraits created above;
 * the timeline is drawn from the same cut list, and the playhead follows the
 * video, so every cut and caption on it is where it really happens.
 */

interface Edl {
  duration: number
  segments: { start: number; end: number; thumb: string; kind: string }[]
  captions: { id: string; start: number; end: number }[]
  text: Record<string, Record<string, string>>
}

export function EditDemo() {
  const { lang, t } = useLocale()
  const e = t.studio.editing
  const [edl, setEdl] = useState<Edl | null>(null)
  const video = useRef<HTMLVideoElement>(null)
  const head = useRef<HTMLDivElement>(null)
  const readout = useRef<HTMLSpanElement>(null)
  const src = `/home/media/edit/edit-${lang}`
  const { reduced } = useAutoplay(video, 0.35, src)

  useEffect(() => {
    let alive = true
    fetch('/home/media/edit/edl.json').then((r) => (r.ok ? r.json() : null)).then((d) => { if (alive) setEdl(d) }).catch(() => {})
    return () => { alive = false }
  }, [])

  // The playhead is written straight to the DOM every frame: no re-render per frame.
  useEffect(() => {
    const v = video.current
    const h = head.current
    const r = readout.current
    if (!v || !h || !edl) return
    const words = edl.text[lang] ?? {}
    let raf = 0
    let shown = ''
    const tick = () => {
      const now = Math.min(v.currentTime, edl.duration)
      h.style.left = `${(now / edl.duration) * 100}%`
      const cap = edl.captions.find((c) => now >= c.start && now < c.end)
      const text = cap ? words[cap.id] ?? '' : ''
      if (r && text !== shown) { r.textContent = text; shown = text }
      raf = requestAnimationFrame(tick)
    }
    tick()
    return () => cancelAnimationFrame(raf)
  }, [edl, src, lang])

  const pct = (x: number) => `${(x / (edl?.duration || 12)) * 100}%`

  return (
    <figure className={s.edit} data-testid="edit-demo">
      <div className={s.editReel}>
        <video key={src} ref={video} muted loop playsInline preload="none" poster={`${src}-poster.jpg`} aria-label={e.alt}>
          <source src={`${src}.mp4`} type="video/mp4" />
        </video>
        {reduced && (
          <button type="button" className={s.filmPlay} onClick={() => video.current?.play().catch(() => {})}>
            <Play size={16} aria-hidden="true" /> {t.replay.play}
          </button>
        )}
      </div>

      {edl && (
        <div className={s.timeline} aria-hidden="true">
          <div className={s.tlBody}>
            <div className={s.tlRuler}>
              {Array.from({ length: Math.floor(edl.duration / 2) + 1 }, (_, i) => (
                <span key={i} style={{ left: pct(i * 2) }}>{`0:${String(i * 2).padStart(2, '0')}`}</span>
              ))}
            </div>
            <div className={s.tlTrack}>
              <b>{e.tracks[0]}</b>
              <div className={s.tlLane}>
                {edl.segments.map((seg, i) => (
                  <i key={i} className={s.tlClip} data-kind={seg.kind} style={{ left: pct(seg.start), width: pct(seg.end - seg.start), backgroundImage: seg.kind === 'card' ? undefined : `url(${seg.thumb})` }} />
                ))}
              </div>
            </div>
            <div className={s.tlTrack}>
              <b>{e.tracks[1]}</b>
              <div className={s.tlLane}>
                {edl.captions.map((c) => (
                  <i key={c.id} className={s.tlCap} style={{ left: pct(c.start), width: pct(c.end - c.start) }} />
                ))}
              </div>
            </div>
            <div className={s.tlTrack}>
              <b>{e.tracks[2]}</b>
              <div className={s.tlLane}>
                {edl.segments.slice(1).map((seg, i) => <i key={i} className={s.tlCut} style={{ left: pct(seg.start) }} />)}
              </div>
            </div>
            <div ref={head} className={s.tlHead} />
          </div>
          <p className={s.tlReadout}><b>{e.tracks[1]}</b> <span ref={readout} /></p>
        </div>
      )}
      <figcaption className={s.replayCaption}><span>{e.caption}</span></figcaption>
    </figure>
  )
}
