'use client'

import { useEffect, useState } from 'react'
import Link from 'next/link'
import { Menu, X } from 'lucide-react'
import { BrandLockup } from '@/components/brand/master'
import { hasSessionHint } from '@/api/client'
import { Container } from './primitives'
import { CTA, SIGNUP_HREF, LOGIN_HREF } from './cta'
import { LanguageGlobe } from './globe'
import { useCopy } from './locale-provider'

/**
 * Header.
 *
 * Five destinations, a language control, one quiet action and one loud one.
 * Everything else a visitor might want is in the page itself — a header that
 * lists every section is a table of contents, and a table of contents is a
 * sign the page needs one.
 *
 * It starts transparent over the hero and only takes a background once the
 * page moves, so the first thing on screen is the product, not a toolbar.
 */
export function SiteNav() {
  const copy = useCopy()
  const [open, setOpen] = useState(false)
  const [scrolled, setScrolled] = useState(false)
  const [signedIn, setSignedIn] = useState(false)

  // Someone already signed in should be offered the way in, not asked to
  // sign in again. Resolved on the client so the HTML stays static.
  useEffect(() => setSignedIn(hasSessionHint()), [])

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 12)
    onScroll()
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    if (!open) return
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false)
    window.addEventListener('keydown', onKey)
    return () => {
      document.body.style.overflow = previous
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  const links = [
    { href: '#product', label: copy.nav.product },
    { href: '#intelligence', label: copy.nav.intelligence },
    { href: '#work', label: copy.nav.work },
    { href: '#agents', label: copy.nav.agents },
    { href: '#pricing', label: copy.nav.pricing },
  ]

  return (
    <header
      className={[
        'fixed inset-x-0 top-0 z-50 transition-all duration-300',
        scrolled
          ? 'border-b border-border/60 bg-bg/80 backdrop-blur-xl'
          : 'border-b border-transparent',
      ].join(' ')}
    >
      <Container>
        <div className="flex h-16 items-center justify-between gap-4 sm:h-[4.5rem]">
          <Link
            href="/"
            className="rounded-lg text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
            aria-label="TRINDATRON"
          >
            <BrandLockup size="md" priority />
          </Link>

          <nav aria-label={copy.nav.product} className="hidden items-center gap-1 lg:flex">
            {links.map((l) => (
              <a
                key={l.href}
                href={l.href}
                className="rounded-lg px-3 py-2 text-sm text-text-secondary transition-colors hover:text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              >
                {l.label}
              </a>
            ))}
          </nav>

          <div className="flex items-center gap-2 sm:gap-3">
            <LanguageGlobe />

            {signedIn ? (
              <CTA href="/home" event="login_click" size="md" className="hidden sm:inline-flex">
                {copy.nav.backToApp}
              </CTA>
            ) : (
              <>
                <CTA
                  href={LOGIN_HREF}
                  event="login_click"
                  variant="quiet"
                  size="md"
                  className="hidden px-3 sm:inline-flex"
                >
                  {copy.nav.signIn}
                </CTA>
                <CTA
                  href={SIGNUP_HREF}
                  event="signup_cta"
                  eventProps={{ place: 'nav' }}
                  size="md"
                  className="hidden sm:inline-flex"
                >
                  {copy.nav.startFree}
                </CTA>
              </>
            )}

            <button
              type="button"
              onClick={() => setOpen((o) => !o)}
              aria-expanded={open}
              aria-controls="site-menu"
              aria-label={open ? copy.nav.closeMenu : copy.nav.openMenu}
              className="inline-flex h-10 w-10 items-center justify-center rounded-xl border border-border text-text-secondary transition-colors hover:text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent lg:hidden"
            >
              {open ? <X className="h-4.5 w-4.5" /> : <Menu className="h-4.5 w-4.5" />}
            </button>
          </div>
        </div>
      </Container>

      {open && (
        <div id="site-menu" className="border-t border-border bg-bg lg:hidden">
          <Container className="py-6">
            <nav className="flex flex-col" aria-label={copy.nav.product}>
              {links.map((l) => (
                <a
                  key={l.href}
                  href={l.href}
                  onClick={() => setOpen(false)}
                  className="border-b border-border-subtle py-3.5 text-[1.05rem] text-text-primary"
                >
                  {l.label}
                </a>
              ))}
            </nav>
            <div className="mt-6 flex flex-col gap-3">
              {signedIn ? (
                <CTA href="/home" event="login_click" size="lg">
                  {copy.nav.backToApp}
                </CTA>
              ) : (
                <>
                  <CTA
                    href={SIGNUP_HREF}
                    event="signup_cta"
                    eventProps={{ place: 'menu' }}
                    size="lg"
                  >
                    {copy.nav.startFree}
                  </CTA>
                  <CTA href={LOGIN_HREF} event="login_click" variant="secondary" size="lg">
                    {copy.nav.signIn}
                  </CTA>
                </>
              )}
            </div>
          </Container>
        </div>
      )}
    </header>
  )
}

/** Reserves the fixed header's height. */
export function NavSpacer() {
  return <div aria-hidden="true" className="h-16 sm:h-[4.5rem]" />
}
