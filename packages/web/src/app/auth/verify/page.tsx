'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'
import { apiVerifyEmail } from '@/api/client'
import { AuthNotice, AuthShell, useLinkToken } from '@/components/auth/auth-shell'
import { Button } from '@/components/ui/button'

type State = 'checking' | 'confirmed' | 'failed'

export default function VerifyEmailPage() {
  const token = useLinkToken()
  const [state, setState] = useState<State>('checking')
  const [error, setError] = useState('')

  useEffect(() => {
    if (token === undefined) return
    if (token === null) {
      setError('Abra o link exatamente como chegou no e-mail.')
      setState('failed')
      return
    }
    apiVerifyEmail(token)
      .then(() => setState('confirmed'))
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : 'Algo deu errado')
        setState('failed')
      })
  }, [token])

  return (
    <AuthShell title={state === 'confirmed' ? 'E-mail confirmado' : 'Confirmar e-mail'}>
      {state === 'checking' ? <p className="text-sm text-text-muted" role="status">Confirmando…</p> : null}
      {state === 'confirmed' ? <AuthNotice tone="ok">Pronto! Seu e-mail está confirmado.</AuthNotice> : null}
      {state === 'failed' ? (
        <AuthNotice tone="error">
          {error} Você pode pedir um novo link em Configurações → Conta.
        </AuthNotice>
      ) : null}
      {state !== 'checking' ? (
        <Link href="/home" className="block">
          <Button variant="primary" className="w-full">Continuar</Button>
        </Link>
      ) : null}
    </AuthShell>
  )
}
