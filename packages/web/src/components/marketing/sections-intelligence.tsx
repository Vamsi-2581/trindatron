'use client'

import { ArrowRight } from 'lucide-react'
import { BrandLockupPlate } from '@/components/brand/master'
import { Container, Section, SectionHeading, Panel } from './primitives'
import { IntelligenceField } from './intelligence-field'
import { Reveal } from './reveal'
import { useCopy } from './locale-provider'

/**
 * Many intelligences, one conversation.
 *
 * The commercial job here is to turn an architectural fact — the router picks
 * a model per request — into something a buyer wants. So the section never
 * mentions routing, models or providers by name: it says the work goes where
 * it is handled best, and that the set keeps growing without the visitor
 * having to do anything about it.
 */
export function Intelligences() {
  const copy = useCopy()

  return (
    <Section id="intelligence" surface="graphite" className="relative overflow-hidden">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 h-[28rem] bg-[radial-gradient(ellipse_at_center_top,rgba(0,194,212,0.07),transparent_68%)]"
      />
      <Container className="relative">
        <div className="grid items-center gap-16 lg:grid-cols-[minmax(0,1fr)_minmax(0,26rem)] lg:gap-20">
          <div>
            <Reveal>
              <SectionHeading
                eyebrow={copy.intelligences.eyebrow}
                title={copy.intelligences.title}
                body={copy.intelligences.body}
              />
            </Reveal>

            <ul className="mt-12 space-y-7">
              {copy.intelligences.points.map((p, i) => (
                <Reveal as="li" key={p.title} delay={i * 90}>
                  <div className="flex gap-4">
                    <span
                      aria-hidden="true"
                      className="mt-2 h-px w-8 shrink-0 bg-accent/60 sm:w-10"
                    />
                    <div>
                      <h3 className="t-1 text-[1.0625rem] font-semibold">{p.title}</h3>
                      <p className="t-2 mt-1.5 max-w-xl text-[0.95rem] leading-relaxed">{p.body}</p>
                    </div>
                  </div>
                </Reveal>
              ))}
            </ul>
          </div>

          <Reveal delay={120} className="order-first lg:order-none">
            <IntelligenceField />
          </Reveal>
        </div>
      </Container>
    </Section>
  )
}

/**
 * The brand moment, and the reasons to trust the work.
 *
 * This is the page's one light surface before pricing, and it exists for two
 * reasons. The first is rhythm: an all-dark page reads as a wall, and the eye
 * needs somewhere to rest between the hero and the second half. The second is
 * the artwork — the master logo is a 3D render on a studio plate, so a light
 * panel is where it can appear at full size as itself rather than as a small
 * mark floated on black.
 *
 * The six points are the architecture, stated as what the visitor gets. Each
 * maps to something real: per-request model selection, the failover chain,
 * per-account isolation checked on every request, approval gates on writing/
 * running/submitting, the isolated execution sandbox, and the activity trail
 * that names the tool used at each step.
 */
export function BrandAndWhy() {
  const copy = useCopy()

  return (
    <Section id="product" surface="paper">
      <Container>
        <div className="grid items-center gap-14 lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)] lg:gap-20">
          <Reveal className="mx-auto w-[62%] max-w-[20rem] sm:w-[46%] lg:w-full">
            <BrandLockupPlate className="h-auto w-full rounded-[1.75rem] shadow-[0_24px_60px_-28px_rgba(11,14,17,0.45)]" />
          </Reveal>

          <div>
            <Reveal>
              <SectionHeading
                eyebrow={copy.why.eyebrow}
                title={copy.why.title}
                body={copy.why.body}
              />
            </Reveal>

            <div className="mt-12 grid gap-x-10 gap-y-8 sm:grid-cols-2">
              {copy.why.points.map((p, i) => (
                <Reveal key={p.title} delay={(i % 2) * 70}>
                  <h3 className="t-1 text-[1rem] font-semibold leading-snug">{p.title}</h3>
                  <p className="t-2 mt-1.5 text-[0.925rem] leading-relaxed">{p.body}</p>
                </Reveal>
              ))}
            </div>
          </div>
        </div>
      </Container>
    </Section>
  )
}

/**
 * Continuity — projects, memory, picking work back up.
 *
 * Sits with Computer use on the same dark run, as the quieter of the two:
 * memory is a reason people stay rather than a reason they arrive.
 */
export function Memory() {
  const copy = useCopy()

  return (
    <Section surface="graphite" tight>
      <Container>
        <div className="grid gap-12 lg:grid-cols-[minmax(0,26rem)_minmax(0,1fr)] lg:gap-20">
          <Reveal>
            <SectionHeading
              eyebrow={copy.memory.eyebrow}
              title={copy.memory.title}
              body={copy.memory.body}
            />
          </Reveal>

          <div className="grid gap-4 sm:grid-cols-3 lg:self-center">
            {copy.memory.points.map((p, i) => (
              <Reveal key={p.title} delay={i * 80}>
                <Panel className="h-full p-6">
                  <h3 className="t-1 text-[0.95rem] font-semibold">{p.title}</h3>
                  <p className="t-3 mt-2 text-[0.875rem] leading-relaxed">{p.body}</p>
                </Panel>
              </Reveal>
            ))}
          </div>
        </div>
      </Container>
    </Section>
  )
}

/**
 * The competitive frame.
 *
 * The hardest thing this page has to do is dislodge an assumption: that AI
 * means a chat box, and that the visitor already has one. So this is the one
 * section that argues rather than describes — two columns, what you are used
 * to against what this does.
 *
 * No competitor is named and none is attacked. Every left-hand row is a
 * property of chat assistants in general, stated without judgement; the
 * argument is that a different category exists, not that anyone else is bad.
 */
export function NewWay() {
  const copy = useCopy()

  return (
    <Section surface="graphite" className="relative overflow-hidden">
      <Container className="relative">
        <Reveal>
          <SectionHeading
            eyebrow={copy.newWay.eyebrow}
            title={copy.newWay.title}
            body={copy.newWay.body}
            align="center"
          />
        </Reveal>

        <div className="mx-auto mt-14 max-w-4xl">
          <div className="mb-4 grid grid-cols-1 gap-4 px-1 sm:grid-cols-2 sm:gap-8">
            <p className="t-3 text-[11px] font-semibold uppercase tracking-[0.18em]">
              {copy.newWay.fromLabel}
            </p>
            <p className="hidden text-[11px] font-semibold uppercase tracking-[0.18em] text-accent sm:block">
              {copy.newWay.toLabel}
            </p>
          </div>

          <ul className="divide-y divide-border border-y border-border">
            {copy.newWay.rows.map((row, i) => (
              <Reveal as="li" key={row.to} delay={i * 60}>
                <div className="grid grid-cols-1 gap-2 py-5 sm:grid-cols-2 sm:gap-8">
                  <p className="t-3 text-[0.95rem] leading-relaxed sm:self-center">{row.from}</p>
                  <p className="t-1 flex items-start gap-3 text-[0.95rem] font-medium leading-relaxed sm:self-center">
                    <ArrowRight
                      className="mt-1 h-3.5 w-3.5 shrink-0 text-accent sm:hidden"
                      aria-hidden="true"
                    />
                    <span>{row.to}</span>
                  </p>
                </div>
              </Reveal>
            ))}
          </ul>
        </div>
      </Container>
    </Section>
  )
}
