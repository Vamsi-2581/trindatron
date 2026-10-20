'use client'

import { ArrowRight } from 'lucide-react'
import { Container } from './primitives'
import { CTA, SIGNUP_HREF } from './cta'
import { WorkSurface } from './work-surface'
import { useCopy } from './locale-provider'

/**
 * Hero.
 *
 * Two things and nothing else: a sentence that says what you get, and the
 * product doing it. No feature list, no logo wall, no explanation of the
 * architecture — those come later, to a reader who has decided to keep going.
 *
 * The backdrop is three flat layers rather than a stack of gradients: a
 * technical grid that dissolves, one cyan light source above the fold, and a
 * chrome horizon where the hero ends. Depth comes from the work surface
 * sitting in front of that light, not from shadows on text.
 */
export function Hero() {
  const copy = useCopy()

  return (
    <section className="graphite relative overflow-hidden">
      <div aria-hidden="true" className="pointer-events-none absolute inset-0">
        <div className="grid-bg absolute inset-0" />
        {/* One light source, placed where the work surface sits. */}
        <div className="absolute right-[-10%] top-[-18rem] h-[42rem] w-[52rem] rounded-full bg-[radial-gradient(ellipse_at_center,rgba(0,194,212,0.13),transparent_62%)] blur-3xl" />
        <div className="absolute left-[-20%] top-[10rem] h-[34rem] w-[40rem] rounded-full bg-[radial-gradient(ellipse_at_center,rgba(154,164,176,0.07),transparent_65%)] blur-3xl" />
      </div>

      <Container className="relative">
        <div className="grid items-center gap-14 pb-24 pt-14 sm:pb-28 sm:pt-20 lg:grid-cols-[minmax(0,1fr)_minmax(0,30rem)] lg:gap-20 lg:pb-24 lg:pt-16">
          {/* min-w-0: a grid item defaults to min-width:auto, so it refuses to
              shrink below its content's min-content width. With a 2.75rem
              headline that is wider than a phone, and the section's
              overflow-hidden then CLIPS the text rather than letting the
              document scroll — so the page measures clean while the words are
              cut off. This one class is the difference. */}
          <div className="min-w-0 max-w-2xl">
            <p className="inline-flex items-center gap-2.5 rounded-full border border-border bg-surface-1/60 px-3.5 py-1.5 text-[0.8rem] text-text-secondary backdrop-blur-sm">
              <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-accent" />
              {copy.hero.kicker}
            </p>

            <h1 className="mt-7 text-balance text-[2.15rem] font-semibold leading-[1.04] tracking-[-0.03em] xs:text-[2.5rem] sm:text-[3.4rem] lg:text-[4.25rem]">
              {/* The first line is chrome, the second is not. Two machined
                  lines would read as an effect; the contrast reads as intent. */}
              {/* The setup line is machined metal; the payoff line is the
                  brand's own cyan. Two greys had no hierarchy — the headline
                  read quieter than the paragraph beneath it, which is the
                  wrong way round for the biggest words on the page. */}
              <span className="chrome-display block">{copy.hero.titleA}</span>
              <span className="block text-accent-light">{copy.hero.titleB}</span>
            </h1>

            <p className="mt-7 max-w-xl text-pretty text-[1.0625rem] leading-[1.65] text-text-secondary sm:text-lg">
              {copy.hero.body}
            </p>

            <div className="mt-10 flex flex-col gap-3 sm:flex-row sm:items-center">
              <CTA
                href={SIGNUP_HREF}
                event="hero_cta"
                eventProps={{ action: 'signup' }}
                size="lg"
                className="w-full sm:w-auto"
              >
                {copy.hero.primary}
                <ArrowRight className="h-4 w-4" aria-hidden="true" />
              </CTA>
              <CTA
                href="#product"
                event="hero_cta"
                eventProps={{ action: 'learn' }}
                variant="secondary"
                size="lg"
                className="w-full sm:w-auto"
              >
                {copy.hero.secondary}
              </CTA>
            </div>

            <p className="mt-5 text-sm text-text-muted">{copy.hero.note}</p>
          </div>

          <div className="min-w-0 lg:pl-4">
            <WorkSurface />
          </div>
        </div>
      </Container>

      {/* Chrome horizon — where the hero ends and the page begins. */}
      <div aria-hidden="true" className="chrome-rule absolute inset-x-0 bottom-0" />
    </section>
  )
}
