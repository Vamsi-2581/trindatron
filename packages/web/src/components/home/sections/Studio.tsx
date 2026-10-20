'use client'

import type { ReactNode } from 'react'
import { MakeDemo } from '../demo/MakeDemo'
import { Replay } from '../demo/Replay'
import { EditDemo } from '../demo/EditDemo'
import { MotionDemo } from '../demo/MotionDemo'
import { AvatarDemo } from '../demo/AvatarDemo'
import { CapFilm } from '../demo/CapFilm'
import type { ChapterId } from '../stage/chapters'
import { useLocale } from '../i18n'
import s from '../home.module.css'

type Row = 'video' | 'images' | 'editing' | 'motion' | 'vision' | 'avatar' | 'slides' | 'docs' | 'computer' | 'code'

/** Ordered by what a visitor understands and wants fastest: what it makes first, then the work it does. */
export const STUDIO: Row[] = ['video', 'images', 'editing', 'motion', 'vision', 'avatar', 'slides', 'docs', 'computer', 'code']

const GALLERY = ['perfume', 'interior', 'food', 'travel']

/**
 * The studio: everything TRINDATRON makes and does, one scene each, with the
 * most visual first. The stage turns rose for what it creates and back to cyan
 * for the work it carries out. Images, video, the presenter and vision are the
 * product's own output; the reel is cut from those clips; presentations,
 * documents, the computer and code are shown as short films of the flow.
 */
export function Studio() {
  const { t } = useLocale()
  const st = t.studio

  const demo: Record<Row, ReactNode> = {
    video: <MakeDemo kind="video" />,
    images: <MakeDemo kind="image" />,
    editing: <EditDemo />,
    motion: <MotionDemo />,
    vision: <Replay demo="vision" />,
    avatar: <AvatarDemo />,
    slides: <CapFilm cap="slides" label={st.slides.alt} />,
    docs: <CapFilm cap="docs" label={st.docs.alt} />,
    computer: <CapFilm cap="computer" label={st.computer.alt} />,
    code: <CapFilm cap="code" label={st.code.alt} />,
  }
  const chapter = (i: number): ChapterId => (i < 6 ? 'create' : 'build')

  return (
    <section id="studio" className={s.studio} aria-labelledby="studio-title">
      <header className={s.studioHead} data-chapter="studio">
        <p className={s.kicker} data-reveal>{st.kicker}</p>
        <h2 id="studio-title" className={s.h2Display} data-reveal>{st.title}</h2>
        <p className={s.lead} data-reveal>{st.sub}</p>
      </header>

      {STUDIO.map((k, i) => {
        const row = st[k]
        return (
          <article key={k} id={`studio-${k}`} className={`${s.sRow} ${i % 2 ? s.sFlip : ''}`} data-k={k} data-chapter={chapter(i)} aria-labelledby={`studio-${k}-title`}>
            <div className={s.sText}>
              <p className={s.capKicker} data-reveal><span>{String(i + 1).padStart(2, '0')}</span> {row.kicker}</p>
              <h3 id={`studio-${k}-title`} className={s.sTitle} data-reveal>{row.t}</h3>
              <p className={s.capBody} data-reveal>{row.d}</p>
              <p className={s.planTag} data-reveal>{row.plan}</p>
              {k === 'images' && (
                <div className={s.gallery} data-reveal>
                  <p className={s.galleryLabel}>{st.images.more}</p>
                  <ul>
                    {GALLERY.map((g, j) => (
                      <li key={g}>
                        {/* eslint-disable-next-line @next/next/no-img-element -- fixed, pre-sized files from public/ */}
                        <img src={`/home/media/g-${g}.jpg`} alt={st.images.gallery[j]} loading="lazy" decoding="async" />
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
            <div className={s.sDemo} data-reveal>{demo[k]}</div>
          </article>
        )
      })}
    </section>
  )
}
