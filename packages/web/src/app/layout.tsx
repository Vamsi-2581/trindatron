import type { Metadata } from 'next'
import { Inter } from 'next/font/google'
import './globals.css'

const inter = Inter({
  subsets: ['latin'],
  variable: '--font-inter',
  display: 'swap',
})

export const metadata: Metadata = {
  metadataBase: new URL('https://trindatron.com'),
  // `%s` is filled by each page's own title; the landing page sets its own.
  title: { default: 'TRINDATRON', template: '%s · TRINDATRON' },
  description: 'An intelligence that understands the work and decides how to carry it out.',
  // Icons come from the file conventions in this directory (icon.png,
  // apple-icon.png) — both generated from the official master artwork in
  // public/brand/master/, so the tab, the home screen and the social card
  // all carry the real mark rather than a stand-in.
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // `lang` is the document's SERVER-rendered language, and the only
    // surface a crawler or a link preview ever sees is the public landing
    // page — which is published in English. So English is the honest
    // default here.
    //
    // It is not a fixed answer: the landing page's LocaleProvider rewrites
    // `documentElement.lang` the moment it resolves the visitor's own
    // language, and the authenticated app (noindex, and already per-user in
    // src/i18n) does the same. This value is what is true before anyone has
    // told us otherwise.
    <html lang="en" className={inter.variable}>
      <body className="h-full bg-bg text-text-primary antialiased font-sans">
        {children}
      </body>
    </html>
  )
}
