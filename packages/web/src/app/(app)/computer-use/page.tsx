'use client'

/**
 * Computer Use. The promise is exactly what the Core can do: the agent works a
 * real browser (and, where the plan and infrastructure allow, a virtual
 * computer) in an isolated environment — never the person's own device — and
 * every action that changes something waits for approval in the conversation.
 * Availability comes from the capability matrix, not from this page.
 */
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { useEffect, useState } from 'react'
import { MousePointerClick, ShieldCheck } from 'lucide-react'
import { apiCreateSession, apiGetCapabilityMatrix, type MatrixEntry } from '@/api/client'
import { Button } from '@/components/ui/button'
import { useAppStore } from '@/store/useAppStore'

const EXAMPLES = [
  'Eu não sei onde fica essa opção. Encontre para mim.',
  'Entre neste site e descubra onde faço meu cadastro.',
  'Encontre o melhor preço e compare.',
  'Abra este formulário e preencha estas informações.',
  'Pesquise isso e me mostre onde encontrou.',
]

export default function ComputerUsePage() {
  const router = useRouter()
  const { prependSession, setPendingMessage } = useAppStore()
  const [matrix, setMatrix] = useState<Record<string, MatrixEntry> | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [request, setRequest] = useState('')
  const [starting, setStarting] = useState(false)

  useEffect(() => {
    apiGetCapabilityMatrix().then(setMatrix).catch(() => setError('Não foi possível verificar o que sua conta pode fazer agora.'))
  }, [])

  const browser = matrix?.browser_control
  const computer = matrix?.computer_control
  const ready = Boolean(browser?.available || computer?.available)

  async function start(text: string) {
    const content = text.trim()
    if (!content || starting) return
    setStarting(true)
    try {
      const session = await apiCreateSession('Computer Use')
      prependSession(session)
      setPendingMessage(content)
      router.push(`/chat/${session.id}?mode=agent`)
    } catch {
      setError('Não foi possível iniciar agora. Tente de novo.')
      setStarting(false)
    }
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-2xl mx-auto px-6 py-12">
        <div className="w-10 h-10 rounded-xl bg-accent/10 flex items-center justify-center mb-5">
          <MousePointerClick className="w-5 h-5 text-accent-light" />
        </div>
        <h1 className="text-2xl font-semibold text-text-primary">Deixe o TRINDATRON usar seu computador por você.</h1>
        <p className="text-base text-text-secondary mt-3">Você não precisa saber onde clicar. Você só precisa saber o que quer fazer.</p>
        <p className="text-sm text-text-muted mt-4">
          Diga o objetivo. O TRINDATRON abre o site, navega, encontra menus e opções, lê, clica, preenche e executa as etapas —
          mostrando cada passo enquanto trabalha.
        </p>

        <div className="mt-6 flex items-start gap-2.5 text-xs text-text-muted bg-surface-1 border border-border rounded-xl px-4 py-3">
          <ShieldCheck className="w-4 h-4 shrink-0 mt-0.5 text-text-secondary" />
          <p>
            Ele trabalha num navegador e num computador virtual isolados, não no seu próprio dispositivo. Antes de clicar, preencher
            ou alterar qualquer coisa, ele para e pede a sua aprovação.
          </p>
        </div>

        {error ? <p className="text-xs text-error mt-4">{error}</p> : null}

        {matrix && !ready ? (
          <div className="mt-8 text-sm text-text-secondary">
            {browser?.status === 'plan' || computer?.status === 'plan' ? (
              <p>Computer Use não está incluído no seu plano atual. <Link href="/plans" className="text-accent-light hover:underline">Ver planos</Link></p>
            ) : (
              <p>Computer Use está indisponível agora. Tente novamente em instantes.</p>
            )}
          </div>
        ) : null}

        {ready ? (
          <>
            <div className="mt-8">
              <textarea
                value={request}
                onChange={(event) => setRequest(event.target.value)}
                rows={3}
                placeholder="O que você quer que o TRINDATRON faça?"
                className="w-full bg-surface-1 border border-border rounded-xl px-4 py-3 text-sm text-text-primary"
              />
              <div className="flex justify-end mt-2">
                <Button loading={starting} disabled={!request.trim()} onClick={() => void start(request)}>Começar</Button>
              </div>
            </div>
            <div className="mt-6">
              <p className="text-[11px] font-semibold text-text-muted uppercase tracking-wider mb-2">Experimente</p>
              <div className="flex flex-col gap-2">
                {EXAMPLES.map((example) => (
                  <button key={example} type="button" onClick={() => setRequest(example)}
                    className="text-left text-sm text-text-secondary bg-surface-1 hover:bg-surface-2 border border-border rounded-lg px-3 py-2 transition-colors">
                    {example}
                  </button>
                ))}
              </div>
            </div>
          </>
        ) : null}
      </div>
    </div>
  )
}
