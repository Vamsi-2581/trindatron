'use client'

import Link from 'next/link'
import { useState, type FormEvent } from 'react'
import { apiForgotPassword } from '@/api/client'
import { AUTH_INPUT, AuthNotice, AuthShell } from '@/components/auth/auth-shell'
import { Button } from '@/components/ui/button'

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState('')
  const [loading, setLoading] = useState(false)
  const [sent, setSent] = useState(false)
  const [error, setError] = useState('')

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await apiForgotPassword(email)
      setSent(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Algo deu errado')
    } finally {
      setLoading(false)
    }
  }

  return (
    <AuthShell
      title="Esqueceu sua senha?"
      description="Digite o e-mail da sua conta. Enviaremos um link para criar uma nova senha."
    >
      {sent ? (
        <AuthNotice tone="ok">
          Se existir uma conta com <strong>{email}</strong>, o link chega em alguns minutos. Ele vale por 30 minutos.
          Confira também a caixa de spam.
        </AuthNotice>
      ) : (
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <label htmlFor="forgot-email" className="block text-xs text-text-muted font-medium">Email</label>
            <input
              id="forgot-email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="voce@email.com"
              required
              autoFocus
              autoComplete="email"
              className={AUTH_INPUT}
            />
          </div>
          {error ? <AuthNotice tone="error">{error}</AuthNotice> : null}
          <Button type="submit" variant="primary" loading={loading} className="w-full">Enviar link</Button>
        </form>
      )}
      <p className="text-center text-xs text-text-muted">
        <Link href="/auth" className="hover:text-text-primary">Voltar para entrar</Link>
      </p>
    </AuthShell>
  )
}
