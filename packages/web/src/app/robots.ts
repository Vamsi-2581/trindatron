import type { MetadataRoute } from 'next'

// The public site is one page; everything behind /auth is a private app and
// has nothing to gain from being crawled.
export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: '*',
        allow: '/',
        disallow: [
          '/auth', '/home', '/chat/', '/code/', '/work/',
          '/projects/', '/agents/', '/agent-instances/',
          '/memory', '/account', '/settings', '/plans',
        ],
      },
    ],
    sitemap: 'https://trindatron.com/sitemap.xml',
    host: 'https://trindatron.com',
  }
}
