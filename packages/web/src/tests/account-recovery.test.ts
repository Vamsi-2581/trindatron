import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createElement } from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

const api = vi.hoisted(() => ({
  apiForgotPassword: vi.fn(),
  apiResetPassword: vi.fn(),
  apiVerifyEmail: vi.fn(),
}))
vi.mock('@/api/client', () => api)

import ForgotPasswordPage from '@/app/auth/forgot/page'
import ResetPasswordPage from '@/app/auth/reset/page'
import VerifyEmailPage from '@/app/auth/verify/page'

function openAt(path: string) {
  window.history.replaceState(null, '', path)
}

describe('account recovery pages', () => {
  beforeEach(() => {
    Object.values(api).forEach((fn) => fn.mockReset())
    openAt('/auth')
  })

  it('"forgot password" answers without saying whether the address has an account', async () => {
    api.apiForgotPassword.mockResolvedValue(undefined)
    render(createElement(ForgotPasswordPage))
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'a@b.com' } })
    fireEvent.click(screen.getByRole('button', { name: 'Enviar link' }))
    expect(await screen.findByText(/Se existir uma conta com/)).toBeInTheDocument()
    expect(api.apiForgotPassword).toHaveBeenCalledWith('a@b.com')
  })

  it('reads the reset token from the link and removes it from the address bar', async () => {
    openAt('/auth/reset#token=secret-token')
    api.apiResetPassword.mockResolvedValue(undefined)
    render(createElement(ResetPasswordPage))
    await waitFor(() => expect(window.location.hash).toBe(''))
    fireEvent.change(screen.getByLabelText('Nova senha'), { target: { value: 'brand-new-pass' } })
    fireEvent.change(screen.getByLabelText('Repita a nova senha'), { target: { value: 'brand-new-pass' } })
    fireEvent.click(screen.getByRole('button', { name: 'Salvar nova senha' }))
    expect(await screen.findByText('Senha alterada')).toBeInTheDocument()
    expect(api.apiResetPassword).toHaveBeenCalledWith('secret-token', 'brand-new-pass')
  })

  it('does not send two different passwords', async () => {
    openAt('/auth/reset#token=secret-token')
    render(createElement(ResetPasswordPage))
    await waitFor(() => expect(window.location.hash).toBe(''))
    fireEvent.change(screen.getByLabelText('Nova senha'), { target: { value: 'brand-new-pass' } })
    fireEvent.change(screen.getByLabelText('Repita a nova senha'), { target: { value: 'other-pass-12' } })
    fireEvent.click(screen.getByRole('button', { name: 'Salvar nova senha' }))
    expect(await screen.findByText('As duas senhas não são iguais.')).toBeInTheDocument()
    expect(api.apiResetPassword).not.toHaveBeenCalled()
  })

  it('a reset link without a token asks for a new one', async () => {
    openAt('/auth/reset')
    render(createElement(ResetPasswordPage))
    expect(await screen.findByText('Link incompleto')).toBeInTheDocument()
  })

  it('confirms the email from the link and says when the link is no good', async () => {
    openAt('/auth/verify#token=good')
    api.apiVerifyEmail.mockResolvedValue(undefined)
    const { unmount } = render(createElement(VerifyEmailPage))
    expect(await screen.findByText('Pronto! Seu e-mail está confirmado.')).toBeInTheDocument()
    expect(api.apiVerifyEmail).toHaveBeenCalledWith('good')
    unmount()

    openAt('/auth/verify#token=used')
    api.apiVerifyEmail.mockRejectedValue(new Error('Este link é inválido ou expirou. Peça um novo.'))
    render(createElement(VerifyEmailPage))
    expect(await screen.findByText(/Este link é inválido ou expirou/)).toBeInTheDocument()
  })
})
