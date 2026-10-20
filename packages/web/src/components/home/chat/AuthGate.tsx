'use client'

import { useEffect, useId, useRef, useState } from 'react'
import { apiLogin, apiRegister } from '@/api/client'
import { useLocale } from '../i18n'
import type { Dict } from '../i18n/en'
import s from '../home.module.css'

export type AuthMode = 'signup' | 'login'

/** The API answers in English; the visitor reads their own language. */
function explain(raw: string, mode: AuthMode, e: Dict['auth']['errors']): { text: string; switchTo?: AuthMode } {
  const m = raw.toLowerCase()
  if (/already|exists|registered|in use/.test(m)) return { text: e.exists, switchTo: 'login' }
  if (/too many/.test(m)) return { text: e.tooMany }
  if (/incorrect|inválid|invalid|senha/.test(m) && mode === 'login') return { text: e.wrong }
  if (/password|senha/.test(m)) return { text: e.password }
  if (/email/.test(m)) return { text: e.email }
  if (/failed to fetch|network|load failed/.test(m)) return { text: e.network }
  return { text: mode === 'signup' ? e.signup : e.login }
}

/**
 * Real sign-up and sign-in, against the same endpoints the app uses
 * (POST /auth/register, POST /auth/login). A new account is a real row in
 * the database; nothing here is simulated.
 */
export function AuthGate({ onAuthed, initialMode = 'signup' }: { onAuthed: (how: AuthMode) => void; initialMode?: AuthMode }) {
  const { t } = useLocale()
  const a = t.auth
  const [mode, setMode] = useState<AuthMode>(initialMode)
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const firstField = useRef<HTMLInputElement>(null)
  const ids = useId()

  useEffect(() => {
    firstField.current?.focus({ preventScroll: true })
  }, [mode])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      if (mode === 'signup') await apiRegister(email.trim(), password, name.trim())
      else await apiLogin(email.trim(), password)
      onAuthed(mode)
    } catch (err) {
      const { text, switchTo } = explain(err instanceof Error ? err.message : '', mode, a.errors)
      setError(text)
      if (switchTo) setMode(switchTo)
      setBusy(false)
    }
  }

  return (
    <div className={s.gate} data-testid="auth-gate">
      <div role="tablist" aria-label={a.tabs} className={s.gateTabs}>
        {(['signup', 'login'] as const).map((m) => (
          <button
            key={m}
            role="tab"
            type="button"
            aria-selected={mode === m}
            onClick={() => { setMode(m); setError(null) }}
            className={s.gateTab}
          >
            {m === 'signup' ? a.signup : a.login}
          </button>
        ))}
      </div>

      <form onSubmit={submit} className={s.gateForm} noValidate>
        {mode === 'signup' && (
          <div className={s.field}>
            <label htmlFor={`${ids}-name`}>{a.name}</label>
            <input ref={firstField} id={`${ids}-name`} autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} required />
          </div>
        )}
        <div className={s.field}>
          <label htmlFor={`${ids}-email`}>{a.email}</label>
          <input
            ref={mode === 'login' ? firstField : undefined}
            id={`${ids}-email`}
            type="email"
            autoComplete="email"
            inputMode="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </div>
        <div className={s.field}>
          <label htmlFor={`${ids}-password`}>{a.password}</label>
          <input
            id={`${ids}-password`}
            type="password"
            autoComplete={mode === 'signup' ? 'new-password' : 'current-password'}
            minLength={8}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          {mode === 'signup' && <p className={s.fieldHint}>{a.passwordHint}</p>}
        </div>

        {error && <p role="alert" className={s.fieldError}>{error}</p>}

        <button
          type="submit"
          disabled={busy || !email || password.length < 8 || (mode === 'signup' && !name.trim())}
          className={`${s.btnPrimary} ${s.btnBlock}`}
        >
          {busy ? a.busy : mode === 'signup' ? a.submitSignup : a.submitLogin}
        </button>
        <p className={s.fieldHint}>{a.free}</p>
      </form>
    </div>
  )
}
