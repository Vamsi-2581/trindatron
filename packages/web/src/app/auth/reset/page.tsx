'use client'

import Link from 'next/link'
import { useState, type FormEvent } from 'react'
import { apiResetPassword } from '@/api/client'
import { AUTH_INPUT, AuthNotice, AuthShell, useLinkToken } from '@/components/auth/auth-shell'
import { Button } from '@/components/ui/button'

export default function ResetPasswordPage() {
  const token = useLinkToken()
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [loading, setLoading] = useState(false)
  const [done, setDone] = useState(false)
  const [error, setError] = useState('')

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    if (password !== confirm) {
      setError('As duas senhas não são iguais.')
      return
    }
    if (!token) return
    setLoading(true)
    try {
      await apiResetPassword(token, password)
      setDone(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Algo deu errado')
    } finally {
      setLoading(false)
    }
  }

  if (done) {
    return (
      <AuthShell title="Senha alterada">
        <AuthNotice tone="ok">
          Sua nova senha já vale. Por segurança, todos os dispositivos foram desconectados.
        </AuthNotice>
        <Link href="/auth" className="block">
          <Button variant="primary" className="w-full">Entrar</Button>
        </Link>
      </AuthShell>
    )
  }

  if (token === null) {
    return (
      <AuthShell title="Link incompleto">
        <AuthNotice tone="error">Abra o link exatamente como chegou no e-mail, ou peça um novo.</AuthNotice>
        <Link href="/auth/forgot" className="block">
          <Button variant="primary" className="w-full">Pedir novo link</Button>
        </Link>
      </AuthShell>
    )
  }

  return (
    <AuthShell title="Crie uma nova senha" description="Use pelo menos 8 caracteres. Evite senhas que você usa em outros sites.">
      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="space-y-1.5">
          <label htmlFor="reset-password" className="block text-xs text-text-muted font-medium">Nova senha</label>
          <input
            id="reset-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            minLength={8}
            autoFocus
            autoComplete="new-password"
            className={AUTH_INPUT}
          />
        </div>
        <div className="space-y-1.5">
          <label htmlFor="reset-confirm" className="block text-xs text-text-muted font-medium">Repita a nova senha</label>
          <input
            id="reset-confirm"
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            required
            minLength={8}
            autoComplete="new-password"
            className={AUTH_INPUT}
          />
        </div>
        {error ? <AuthNotice tone="error">{error}</AuthNotice> : null}
        <Button type="submit" variant="primary" loading={loading} disabled={token === undefined} className="w-full">
          Salvar nova senha
        </Button>
      </form>
      {error.includes('expirou') ? (
        <p className="text-center text-xs text-text-muted">
          <Link href="/auth/forgot" className="hover:text-text-primary">Pedir novo link</Link>
        </p>
      ) : null}
    </AuthShell>
  )
}
