'use client'

import { useEffect, useState, type ReactNode } from 'react'
import { TrinityLogo } from '@/components/brand/trinity-mark'

export const AUTH_INPUT =
  'w-full bg-surface-2 border border-border rounded-xl px-3.5 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent/60 transition-colors'

/** The brand and card every /auth page shares. */
export function AuthShell({ title, description, children }: { title: string; description?: string; children: ReactNode }) {
  return (
    <div className="w-full max-w-sm">
      <div className="flex flex-col items-center mb-10">
        <TrinityLogo size="md" />
      </div>
      <div className="bg-surface-1 border border-border rounded-2xl p-6 space-y-4">
        <div className="space-y-1.5">
          <h1 className="text-base font-semibold text-text-primary">{title}</h1>
          {description ? <p className="text-sm text-text-muted leading-relaxed">{description}</p> : null}
        </div>
        {children}
      </div>
    </div>
  )
}

export function AuthNotice({ tone, children }: { tone: 'error' | 'ok'; children: ReactNode }) {
  const classes = tone === 'error'
    ? 'text-error bg-error/5 border-error/20'
    : 'text-text-primary bg-accent/5 border-accent/20'
  return (
    <div role={tone === 'error' ? 'alert' : 'status'} className={`text-xs border rounded-xl px-3.5 py-2.5 leading-relaxed ${classes}`}>
      {children}
    </div>
  )
}

/**
 * The single-use token an email link carries after "#token=". It is in the
 * fragment so it never reaches a server log or a Referer header; once read it
 * is removed from the address bar and history.
 * `undefined` while not yet read, `null` when the link carried none.
 */
export function useLinkToken(): string | null | undefined {
  const [token, setToken] = useState<string | null | undefined>(undefined)
  useEffect(() => {
    const found = new URLSearchParams(window.location.hash.slice(1)).get('token')
    setToken(found || null)
    if (window.location.hash) window.history.replaceState(null, '', window.location.pathname)
  }, [])
  return token
}
