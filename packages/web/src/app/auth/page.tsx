'use client'

import Link from 'next/link'
import { Suspense, useState, type FormEvent } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { useAppStore } from '@/store/useAppStore'
import { apiLogin, apiRegister, apiGetMe, googleSignInUrl } from '@/api/client'
import { TrinityLogo } from '@/components/brand/trinity-mark'
import { Button } from '@/components/ui/button'

type Tab = 'login' | 'register'

/** The API returns here with ?error=<code> and never with detail. */
const GOOGLE_ERRORS: Record<string, string> = {
  google_cancelled: 'Login com Google cancelado.',
  google_failed: 'Não foi possível entrar com Google. Tente novamente.',
  google_unavailable: 'Login com Google indisponível no momento.',
  google_account_disabled: 'Esta conta está desativada.',
  google_rate_limited: 'Muitas tentativas. Aguarde um pouco e tente novamente.',
}

function GoogleMark() {
  return (
    <svg viewBox="0 0 48 48" className="w-4 h-4" aria-hidden="true">
      <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z" />
      <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z" />
      <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z" />
      <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z" />
    </svg>
  )
}

export default function AuthPage() {
  // useSearchParams needs a Suspense boundary during prerendering.
  return (
    <Suspense fallback={null}>
      <AuthForm />
    </Suspense>
  )
}

function AuthForm() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const { setAuth } = useAppStore()
  // The public site's "Testar gratis" CTA links to /auth?signup=1 so a
  // visitor who came to create an account does not land on the login tab
  // and have to find the switch. Any other entry point still opens login.
  const [tab, setTab] = useState<Tab>(
    searchParams.get('signup') === '1' ? 'register' : 'login',
  )
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [fullName, setFullName] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(GOOGLE_ERRORS[searchParams.get('error') ?? ''] ?? '')

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)

    try {
      const resp = tab === 'login'
        ? await apiLogin(email, password)
        : await apiRegister(email, password, fullName)

      const me = await apiGetMe()
      setAuth(resp.access_token, { email: me.email, plan: me.plan })
      router.replace('/home')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Algo deu errado')
    } finally {
      setLoading(false)
    }
  }

  function switchTab(t: Tab) {
    setTab(t)
    setError('')
  }

  return (
    <div className="w-full max-w-sm">
      {/* Brand */}
      <div className="flex flex-col items-center mb-10">
        <div className="mb-3"><TrinityLogo size="md" /></div>
        <p className="text-sm text-text-muted text-center leading-relaxed">
          Uma inteligência que entende o trabalho<br />e decide como executá-lo.
        </p>
      </div>

      {/* Card */}
      <div className="bg-surface-1 border border-border rounded-2xl overflow-hidden">
        {/* Tab bar */}
        <div className="flex border-b border-border">
          {(['login', 'register'] as Tab[]).map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => switchTab(t)}
              className={[
                'flex-1 py-3 text-sm font-medium transition-colors',
                tab === t
                  ? 'text-text-primary border-b-2 border-accent -mb-px'
                  : 'text-text-muted hover:text-text-secondary',
              ].join(' ')}
            >
              {t === 'login' ? 'Entrar' : 'Criar conta'}
            </button>
          ))}
        </div>

        <div className="px-6 pt-6">
          <a
            href={googleSignInUrl()}
            className="flex w-full items-center justify-center gap-2.5 rounded-xl border border-border bg-surface-2 px-3.5 py-2.5 text-sm font-medium text-text-primary transition-colors hover:border-accent/60"
          >
            <GoogleMark />
            Continuar com Google
          </a>
          <div className="flex items-center gap-3 pt-5 text-[11px] uppercase tracking-wider text-text-muted">
            <span className="h-px flex-1 bg-border" />
            ou
            <span className="h-px flex-1 bg-border" />
          </div>
        </div>

        <form onSubmit={handleSubmit} className="p-6 pt-4 space-y-4">
          {tab === 'register' && (
            <div className="space-y-1.5">
              <label className="block text-xs text-text-muted font-medium">Nome</label>
              <input
                type="text"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                placeholder="Seu nome"
                autoComplete="name"
                className="w-full bg-surface-2 border border-border rounded-xl px-3.5 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent/60 transition-colors"
              />
            </div>
          )}

          <div className="space-y-1.5">
            <label className="block text-xs text-text-muted font-medium">Email</label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="voce@email.com"
              required
              autoFocus={tab === 'login'}
              autoComplete="email"
              className="w-full bg-surface-2 border border-border rounded-xl px-3.5 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent/60 transition-colors"
            />
          </div>

          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <label className="block text-xs text-text-muted font-medium">Senha</label>
              {tab === 'login' && (
                <Link href="/auth/forgot" className="text-xs text-text-muted hover:text-text-primary">
                  Esqueci minha senha
                </Link>
              )}
            </div>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              required
              minLength={8}
              autoComplete={tab === 'login' ? 'current-password' : 'new-password'}
              className="w-full bg-surface-2 border border-border rounded-xl px-3.5 py-2.5 text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:border-accent/60 transition-colors"
            />
          </div>

          {error && (
            <div className="text-error text-xs bg-error/5 border border-error/20 rounded-xl px-3.5 py-2.5 leading-relaxed">
              {error}
            </div>
          )}

          <Button
            type="submit"
            variant="primary"
            loading={loading}
            className="w-full mt-2"
          >
            {tab === 'login' ? 'Entrar' : 'Criar conta'}
          </Button>
        </form>
      </div>

      <p className="text-center text-[11px] text-text-muted mt-6 leading-relaxed">
        Ao continuar você concorda com os Termos de Uso e Política de Privacidade.
      </p>
    </div>
  )
}
