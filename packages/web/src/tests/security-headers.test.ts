/**
 * The security headers the app ships with, tested as the product contract
 * they are.
 *
 * These are not decorative. Two features were silently dead in production
 * because of them, and neither failed anywhere a developer would look:
 *
 *   - read-aloud: /voice/synthesize returned a perfectly good `audio/wav`
 *     body, the client wrapped it in a Blob, and the browser refused to load
 *     the object URL because there was no `media-src` and `default-src
 *     'self'` does not cover `blob:`. The user saw "Failed to load because
 *     no supported source was found" — a message about a container problem
 *     that did not exist.
 *   - voice input: the composer calls getUserMedia, but Permissions-Policy
 *     shipped `microphone=()`, an empty allowlist that disables the mic for
 *     this origin too.
 *
 * Both are invisible in dev (no headers) and invisible to unit tests of the
 * components themselves. This file is where they get caught.
 */
import { describe, it, expect, beforeAll } from 'vitest'

type Header = { key: string; value: string }

let headers: Record<string, string>
let csp: Record<string, string[]>

beforeAll(async () => {
  const mod = await import('../../next.config.mjs')
  const config = mod.default as { headers: () => Promise<{ headers: Header[] }[]> }
  const rules = await config.headers()
  headers = Object.fromEntries(rules[0].headers.map((h) => [h.key, h.value]))

  csp = Object.fromEntries(
    headers['Content-Security-Policy']
      .split(';')
      .map((d) => d.trim())
      .filter(Boolean)
      .map((d) => {
        const [name, ...sources] = d.split(/\s+/)
        return [name, sources]
      }),
  )
})

describe('Content-Security-Policy', () => {
  it('allows the app to play audio it created itself', () => {
    expect(csp['media-src']).toBeDefined()
    expect(csp['media-src']).toContain("blob:")
    expect(csp['media-src']).toContain("'self'")
  })

  it('does not leave media to fall back to default-src', () => {
    // The whole bug: with media-src absent the browser uses default-src,
    // which is 'self' and blocks blob:.
    expect(csp['default-src']).toEqual(["'self'"])
    expect(Object.keys(csp)).toContain('media-src')
  })

  it('does not open media up to arbitrary origins', () => {
    // Created videos are served from object storage through signed links, so
    // that one host family is allowed next to 'self' and blob: — nothing else.
    const allowed = ["'self'", 'blob:', 'https://*.r2.cloudflarestorage.com']
    expect(csp['media-src']).not.toContain('*')
    expect(csp['media-src']!.every((s) => allowed.includes(s))).toBe(true)
  })

  it('still restricts where the app may talk to', () => {
    expect(csp['connect-src']).toContain("'self'")
    expect(csp['connect-src']).not.toContain('*')
  })

  it('keeps the protections that were already there', () => {
    expect(csp['frame-ancestors']).toEqual(["'none'"])
    expect(csp['object-src']).toEqual(["'none'"])
    expect(csp['base-uri']).toEqual(["'self'"])
  })
})

describe('Permissions-Policy', () => {
  it('lets this origin use the microphone the composer records with', () => {
    expect(headers['Permissions-Policy']).toContain('microphone=(self)')
  })

  it('keeps camera and geolocation fully disabled', () => {
    // Nothing in the product uses either; an empty allowlist is correct here.
    expect(headers['Permissions-Policy']).toContain('camera=()')
    expect(headers['Permissions-Policy']).toContain('geolocation=()')
  })
})

describe('transport headers', () => {
  it('keeps clickjacking and MIME-sniffing defences', () => {
    expect(headers['X-Frame-Options']).toBe('DENY')
    expect(headers['X-Content-Type-Options']).toBe('nosniff')
    expect(headers['Referrer-Policy']).toBe('strict-origin-when-cross-origin')
  })
})
