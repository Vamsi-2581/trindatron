import type { Metadata } from 'next'
import { Geist, Geist_Mono, Instrument_Serif } from 'next/font/google'
import { Home } from '@/components/home/Home'

/**
 * The public TRINDATRON Home (components/home). English is the default and
 * what the server renders; the visitor can switch to Portuguese or Spanish.
 * The symbol on the stage is the master from "IMAGENS LOGO/3. Modelo
 * Vertical", never redrawn (see public/brand/README.md).
 */

const geist = Geist({ subsets: ['latin'], variable: '--font-geist', display: 'swap' })
const geistMono = Geist_Mono({ subsets: ['latin'], variable: '--font-geist-mono', display: 'swap' })
const serif = Instrument_Serif({ subsets: ['latin'], weight: '400', style: ['normal', 'italic'], variable: '--font-serif', display: 'swap' })

const TITLE = 'TRINDATRON. You speak. It does it.'
const DESCRIPTION =
  "Tell TRINDATRON what you need, the way you'd tell a person. It searches the internet, opens real sites, reads your files and gets it done."

export const metadata: Metadata = {
  metadataBase: new URL('https://trindatron.com'),
  title: { absolute: TITLE },
  description: DESCRIPTION,
  alternates: { canonical: 'https://trindatron.com/' },
  openGraph: {
    type: 'website',
    url: 'https://trindatron.com/',
    siteName: 'TRINDATRON',
    locale: 'en_US',
    alternateLocale: ['pt_BR', 'es_ES'],
    title: TITLE,
    description: DESCRIPTION,
  },
  twitter: { card: 'summary_large_image', title: TITLE, description: DESCRIPTION },
  robots: { index: true, follow: true },
}

export default function HomePage() {
  return (
    <>
      <Home fontClass={`${geist.variable} ${geistMono.variable} ${serif.variable}`} />
      <StructuredData />
    </>
  )
}

/** Only facts stated on the page itself. */
function StructuredData() {
  const json = {
    '@context': 'https://schema.org',
    '@graph': [
      {
        '@type': 'Organization',
        '@id': 'https://trindatron.com/#organization',
        name: 'TRINDATRON',
        url: 'https://trindatron.com/',
        logo: 'https://trindatron.com/brand/master/trindatron-icon-512.png',
      },
      {
        '@type': 'WebSite',
        '@id': 'https://trindatron.com/#website',
        url: 'https://trindatron.com/',
        name: 'TRINDATRON',
        inLanguage: ['en', 'pt-BR', 'es'],
        publisher: { '@id': 'https://trindatron.com/#organization' },
      },
    ],
  }
  return <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(json) }} />
}
