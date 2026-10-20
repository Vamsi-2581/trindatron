'use client'

import { useEffect, useRef } from 'react'
import { useRouter } from 'next/navigation'
import { useAppStore } from '@/store/useAppStore'
import { apiGetMe, bootstrapSession, getStoredToken } from '@/api/client'
import { TrinityMark } from '@/components/brand/trinity-mark'

/**
 * Where the API sends the browser after a successful Google sign-in. The
 * refresh cookie is already set; this page exchanges it for an access token
 * exactly as a reload would, then enters the app.
 */
export default function GoogleSignInComplete() {
  const router = useRouter()
  const { setAuth } = useAppStore()
  const started = useRef(false)

  useEffect(() => {
    if (started.current) return
    started.current = true

    async function complete() {
      try {
        if (!(await bootstrapSession())) throw new Error('no session')
        const me = await apiGetMe()
        setAuth(getStoredToken() ?? '', { email: me.email, plan: me.plan })
        router.replace('/home')
      } catch {
        router.replace('/auth?error=google_failed')
      }
    }

    complete()
  }, [router, setAuth])

  return (
    <div className="flex h-40 items-center justify-center">
      <TrinityMark className="w-8 h-6 text-accent animate-pulse" />
    </div>
  )
}
