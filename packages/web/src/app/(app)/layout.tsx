'use client'

import { useEffect, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useAppStore } from '@/store/useAppStore'
import { bootstrapSession, getStoredToken, onSessionExpired, apiGetMe, apiListSessions, apiGetEntitlement, apiUpdateTimezone } from '@/api/client'
import { getLocale } from '@/i18n'
import { Sidebar } from '@/components/layout/sidebar'
import { TrinityMark } from '@/components/brand/trinity-mark'

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter()
  const { setAuth, clearAuth, setSessions, setEntitlement } = useAppStore()
  const [ready, setReady] = useState(false)
  const booted = useRef(false)

  // A refused refresh anywhere in the app (revoked on another device, or
  // expired) returns the user to sign-in instead of failing request by request.
  useEffect(() => onSessionExpired(() => {
    clearAuth()
    router.replace('/auth')
  }), [clearAuth, router])

  // The document is served as lang="en" because the only page a crawler
  // reads is the public landing page, which is English. Inside the app the
  // language is the user's own, so correct it as soon as we are mounted —
  // otherwise a screen reader announces a Portuguese interface in English.
  useEffect(() => {
    document.documentElement.lang = getLocale() === 'pt' ? 'pt-BR' : getLocale()
  }, [])

  useEffect(() => {
    if (booted.current) return
    booted.current = true

    async function boot() {
      if (!(await bootstrapSession())) {
        router.replace('/auth')
        return
      }
      try {
        const [me, { sessions }, entitlement] = await Promise.all([
          apiGetMe(),
          apiListSessions(),
          apiGetEntitlement().catch(() => null),
        ])
        setAuth(getStoredToken() ?? '', { email: me.email, plan: me.plan })
        setSessions(sessions)
        if (entitlement) setEntitlement(entitlement)
        setReady(true)

        // Detect and persist the browser's IANA timezone once per app boot
        // (guarded by `booted` above — never in a loop). Only writes when
        // it actually differs from what's stored, to avoid a redundant
        // PATCH on every login. Drives the weekly reset rule server-side —
        // never assume Brasília or any fixed zone.
        try {
          const detected = Intl.DateTimeFormat().resolvedOptions().timeZone
          if (detected && detected !== me.timezone) {
            apiUpdateTimezone(detected).catch(() => {
              // Non-fatal — the weekly window falls back to UTC server-side
              // until this succeeds on a later boot.
            })
          }
        } catch {
          // Intl API unavailable — extremely rare; same fallback applies.
        }
      } catch {
        router.replace('/auth')
      }
    }

    boot()
  }, [router, setAuth, setSessions])

  if (!ready) {
    return (
      <div className="flex h-screen items-center justify-center bg-bg">
        <TrinityMark className="w-8 h-6 text-accent animate-pulse" />
      </div>
    )
  }

  return (
    <div className="flex h-screen bg-bg overflow-hidden">
      <Sidebar />
      <main className="flex-1 overflow-hidden flex flex-col">{children}</main>
    </div>
  )
}
