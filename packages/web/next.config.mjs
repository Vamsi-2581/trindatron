/** @type {import('next').NextConfig} */

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'
const isDev = process.env.NODE_ENV === 'development'

// connect-src must include the API (cross-origin fetch + SSE) and, in dev,
// the Next.js HMR WebSocket so hot-reload keeps working.
// Created images and videos are private files in object storage (Cloudflare R2). The chat card loads them
// through short-lived signed links (GET /media/artifacts/{id}/link), path-style on the account's endpoint,
// so the page must allow that host for <img>, <video> and the share button's fetch.
const MEDIA_STORAGE = 'https://*.r2.cloudflarestorage.com'
const connectSrc = ["'self'", API_URL, MEDIA_STORAGE]
if (isDev) {
  connectSrc.push('ws://localhost:3000', `ws://localhost:${process.env.PORT ?? 3000}`)
}

// Next.js 14 App Router without a nonce-generating middleware requires
// 'unsafe-inline' for scripts: the framework injects __NEXT_DATA__ and
// hydration chunks as inline <script> tags, especially in dev mode.
// In production a proper nonce-based CSP (via Next.js middleware) would
// remove this directive, but that requires Fase 7+ work.
// The meaningful protections here are: connect-src restricts API origin,
// frame-ancestors blocks clickjacking, and object-src blocks plugin injection.
const scriptSrc = isDev
  ? "'self' 'unsafe-inline' 'unsafe-eval'" // HMR needs eval; inline for hydration
  : "'self' 'unsafe-inline'"                // production inline hydration chunks

const securityHeaders = [
  {
    key: 'Content-Security-Policy',
    value: [
      "default-src 'self'",
      `script-src ${scriptSrc}`,
      // Tailwind v3 injects utility classes via <style> at runtime.
      "style-src 'self' 'unsafe-inline'",
      `connect-src ${connectSrc.join(' ')}`,
      // data: for inline SVG icons; fonts.gstatic.com for Inter via Google Fonts.
      `img-src 'self' data: blob: ${MEDIA_STORAGE}`,
      "font-src 'self' https://fonts.gstatic.com",
      // blob: is REQUIRED for read-aloud. /voice/synthesize returns audio
      // bytes, the client wraps them in a Blob and plays an object URL —
      // and with no media-src directive the browser fell back to
      // default-src 'self', which does not cover blob:. The console said so
      // in as many words ("Loading media from 'blob:...' violates ...
      // default-src 'self'. Note that 'media-src' was not explicitly set"),
      // and the <audio> element surfaced it as "no supported source". This
      // is narrower than the fallback it replaces: media may come only from
      // this origin or from a blob this page itself created.
      `media-src 'self' blob: ${MEDIA_STORAGE}`,
      // Prevent this app from being embedded in iframes (clickjacking defence).
      "frame-ancestors 'none'",
      // Belt-and-suspenders: disallow plugins and dynamic base-URL overrides.
      "object-src 'none'",
      "base-uri 'self'",
    ].join('; '),
  },
  // Belt-and-suspenders against iframe embedding (older browsers ignore CSP frame-ancestors).
  { key: 'X-Frame-Options',    value: 'DENY' },
  // Prevent MIME-type sniffing.
  { key: 'X-Content-Type-Options', value: 'nosniff' },
  // Don't send the full Referer URL to third-party origins.
  { key: 'Referrer-Policy',    value: 'strict-origin-when-cross-origin' },
  // Lock down sensitive browser permissions. `microphone=(self)` rather than
  // `()`: the composer records voice for /voice/transcribe
  // (navigator.mediaDevices.getUserMedia), and an empty allowlist disabled
  // that for this origin too — the feature could never have worked in
  // production. Camera and geolocation stay fully off; nothing uses them.
  { key: 'Permissions-Policy', value: 'camera=(), microphone=(self), geolocation=()' },
]

const nextConfig = {
  async headers() {
    return [
      {
        source: '/(.*)',
        headers: securityHeaders,
      },
    ]
  },
}

export default nextConfig
