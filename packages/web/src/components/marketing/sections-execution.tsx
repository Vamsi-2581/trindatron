'use client'

import { useEffect, useRef, useState } from 'react'
import { Check, ShieldCheck } from 'lucide-react'
import { Container, Section, SectionHeading, Panel, StatusPill } from './primitives'
import { Reveal } from './reveal'
import { useCopy } from './locale-provider'

/**
 * Work — the section that has to feel like a capability rather than a manual.
 *
 * The five steps are the shape of a real run: understand, plan, execute,
 * check, deliver. They light up as the section scrolls past, which is the
 * one animation on the page that carries information — you watch the work
 * advance, which is exactly what the section is claiming happens.
 *
 * The control note is the same fact the old page opened with as a warning
 * about autonomy. Stated as what you keep rather than what you risk, it
 * belongs on a sales page; stated the other way it belonged in a disclaimer.
 */
export function Work() {
  const copy = useCopy()
  const [active, setActive] = useState(-1)
  const railRef = useRef<HTMLOListElement>(null)

  // Advance the rail as the list crosses the middle of the viewport. One
  // observer for the whole list, not one per step.
  useEffect(() => {
    const el = railRef.current
    if (!el) return
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    if (reduced || typeof IntersectionObserver === 'undefined') {
      setActive(copy.work.steps.length - 1)
      return
    }
    const items = Array.from(el.querySelectorAll('[data-step]'))
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue
          const i = Number((entry.target as HTMLElement).dataset.step)
          setActive((prev) => (i > prev ? i : prev))
        }
      },
      { rootMargin: '-45% 0px -45% 0px' },
    )
    items.forEach((i) => io.observe(i))
    return () => io.disconnect()
  }, [copy.work.steps.length])

  const progress = active < 0 ? 0 : (active + 1) / copy.work.steps.length

  return (
    <Section id="work" surface="graphite">
      <Container>
        <Reveal>
          <SectionHeading
            eyebrow={copy.work.eyebrow}
            title={copy.work.title}
            body={copy.work.body}
          />
        </Reveal>

        <div className="mt-16 grid gap-12 lg:grid-cols-[minmax(0,1fr)_minmax(0,22rem)] lg:gap-20">
          <ol ref={railRef} className="relative pl-10 sm:pl-12">
            {/* The rail. Its fill is the progress of the work. */}
            <span
              aria-hidden="true"
              className="absolute left-[0.6875rem] top-2 h-[calc(100%-1rem)] w-px bg-border sm:left-[0.9375rem]"
            />
            <span
              aria-hidden="true"
              className="rail-fill absolute left-[0.6875rem] top-2 h-[calc(100%-1rem)] w-px bg-accent sm:left-[0.9375rem]"
              style={{ transform: `scaleY(${progress})` }}
            />

            {copy.work.steps.map((s, i) => {
              const on = i <= active
              return (
                <li key={s.label} data-step={i} className="relative pb-9 last:pb-0">
                  <span
                    aria-hidden="true"
                    className={[
                      'absolute left-[-2.5rem] top-0.5 flex h-6 w-6 items-center justify-center rounded-full border transition-all duration-500 sm:left-[-3rem] sm:h-8 sm:w-8',
                      on
                        ? 'border-accent/60 bg-accent/15 text-accent-light'
                        : 'border-border bg-bg text-transparent',
                    ].join(' ')}
                  >
                    {on ? (
                      <Check className="h-3.5 w-3.5" />
                    ) : (
                      <span className="h-1.5 w-1.5 rounded-full bg-border" />
                    )}
                  </span>
                  <h3
                    className={[
                      'text-[1.15rem] font-semibold transition-colors duration-500 sm:text-[1.3rem]',
                      on ? 'text-text-primary' : 'text-text-muted',
                    ].join(' ')}
                  >
                    {s.label}
                  </h3>
                  <p className="t-2 mt-1.5 max-w-lg text-[0.95rem] leading-relaxed">{s.detail}</p>
                </li>
              )
            })}
          </ol>

          <Reveal delay={100} className="lg:self-center">
            <Panel className="p-7">
              <ShieldCheck className="h-6 w-6 text-accent-light" aria-hidden="true" />
              <h3 className="t-1 mt-4 text-[1.15rem] font-semibold">{copy.work.controlTitle}</h3>
              <p className="t-2 mt-3 text-[0.95rem] leading-relaxed">{copy.work.controlBody}</p>

              {/* The approval moment, shown rather than described. */}
              <div className="mt-6 flex items-center gap-2.5 rounded-xl border border-border bg-surface-2/60 p-3">
                <span className="flex-1 truncate text-[0.8rem] text-text-secondary">
                  {copy.work.steps[2]?.label}
                </span>
                <span className="rounded-lg border border-border px-2.5 py-1 text-[0.7rem] font-medium text-text-secondary">
                  {copy.work.review}
                </span>
                <span className="rounded-lg bg-accent px-2.5 py-1 text-[0.7rem] font-semibold text-text-inverse">
                  {copy.work.approve}
                </span>
              </div>
            </Panel>
          </Reveal>
        </div>
      </Container>
    </Section>
  )
}

/**
 * Agents — delegation, not automation jargon.
 */
export function Agents() {
  const copy = useCopy()

  return (
    <Section id="agents" surface="graphite" tight>
      <Container>
        <Reveal>
          <SectionHeading
            eyebrow={copy.agents.eyebrow}
            title={copy.agents.title}
            body={copy.agents.body}
          />
        </Reveal>

        <div className="mt-14 grid gap-5 md:grid-cols-3">
          {copy.agents.points.map((p, i) => (
            <Reveal key={p.title} delay={i * 90}>
              <Panel interactive className="h-full p-7">
                <span
                  aria-hidden="true"
                  className="block h-px w-9 bg-accent/70"
                />
                <h3 className="t-1 mt-5 text-[1.05rem] font-semibold">{p.title}</h3>
                <p className="t-2 mt-2.5 text-[0.925rem] leading-relaxed">{p.body}</p>
              </Panel>
            </Reveal>
          ))}
        </div>
      </Container>
    </Section>
  )
}

/**
 * Computer use.
 *
 * The strategic capability, and the one most easily oversold. What is listed
 * under "working today" is exactly what the agent registry wires into a real
 * run: a browser that navigates, reads, clicks, fills and screenshots; files;
 * and a shell — all inside an isolated workspace.
 *
 * Operating the visitor's own desktop is a later stage of the product and is
 * presented as such, in its own column, described as where the capability is
 * going. It is never phrased as something the product does now.
 */
export function ComputerUse() {
  const copy = useCopy()

  return (
    <Section id="reach" surface="graphite">
      <Container>
        <Reveal>
          <SectionHeading
            eyebrow={copy.reach.eyebrow}
            title={copy.reach.title}
            body={copy.reach.body}
          />
        </Reveal>

        <div className="mt-14 grid gap-10 lg:grid-cols-[minmax(0,1fr)_minmax(0,20rem)] lg:gap-16">
          <div className="grid gap-5 sm:grid-cols-3">
            {copy.reach.live.map((p, i) => (
              <Reveal key={p.title} delay={i * 80}>
                <Panel className="h-full p-6">
                  <StatusPill status="live" live={copy.reach.liveLabel} next={copy.reach.nextLabel} />
                  <h3 className="t-1 mt-4 text-[1rem] font-semibold">{p.title}</h3>
                  <p className="t-2 mt-2 text-[0.9rem] leading-relaxed">{p.body}</p>
                </Panel>
              </Reveal>
            ))}
          </div>

          <div className="grid gap-5">
            {copy.reach.next.map((p, i) => (
              <Reveal key={p.title} delay={i * 80}>
                <Panel className="h-full border-dashed p-6">
                  <StatusPill status="next" live={copy.reach.liveLabel} next={copy.reach.nextLabel} />
                  <h3 className="t-1 mt-4 text-[1rem] font-semibold">{p.title}</h3>
                  <p className="t-2 mt-2 text-[0.9rem] leading-relaxed">{p.body}</p>
                </Panel>
              </Reveal>
            ))}
          </div>
        </div>
      </Container>
    </Section>
  )
}
