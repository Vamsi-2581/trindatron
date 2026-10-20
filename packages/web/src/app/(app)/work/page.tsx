'use client'

/**
 * Work workspaces are real in the Core (/workspaces/work: execute, approve,
 * reject, connectors) and /work/[id] drives one. What does not exist anywhere
 * in this app is a way to create one — apiCreateWorkWorkspace has no call site
 * — so no screen can open one today.
 *
 * That is what this page says. It used to show a feature grid and an "Em breve"
 * badge, which promised a product rather than stating where the gap is. The
 * same work, in the form that does exist today, is agent mode in a conversation.
 */
import Link from 'next/link'
import { Briefcase, Lock } from 'lucide-react'
import { useAppStore } from '@/store/useAppStore'

export default function WorkPage() {
  const { entitlement } = useAppStore()
  const included = !entitlement || entitlement.features.supports_trinity_work

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-2xl mx-auto px-6 py-12">
        <div className="w-11 h-11 rounded-xl bg-surface-2 border border-border flex items-center justify-center mb-5">
          <Briefcase className="w-5 h-5 text-text-secondary" />
        </div>
        <h1 className="text-xl font-semibold text-text-primary">Execução autônoma</h1>

        {included ? (
          <>
            <p className="text-sm text-text-muted mt-2 leading-relaxed">
              Tarefas longas executadas em etapas, com aprovação antes de cada ação que altera algo.
            </p>
            <p className="text-sm text-text-secondary mt-6 leading-relaxed">
              Ainda não há como iniciar um espaço de execução por aqui. O que já funciona hoje, com o mesmo controle de
              aprovação, é o modo agente dentro de uma conversa — e o Computer Use, quando a tarefa é usar um navegador.
            </p>
            <div className="flex flex-wrap gap-2 mt-6">
              <Link
                href="/agents"
                className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-accent/10 border border-accent/30 text-sm text-accent-light hover:bg-accent/15 transition-colors"
              >
                Meus agentes
              </Link>
              <Link
                href="/computer-use"
                className="inline-flex items-center gap-2 px-4 py-2 rounded-xl bg-surface-1 border border-border text-sm text-text-secondary hover:text-text-primary hover:bg-surface-2 transition-colors"
              >
                Computer Use
              </Link>
            </div>
          </>
        ) : (
          <div className="mt-6 flex items-start gap-3 px-4 py-4 rounded-xl bg-surface-1 border border-border">
            <Lock className="w-4 h-4 text-text-muted shrink-0 mt-0.5" />
            <div>
              <p className="text-sm text-text-primary">Seu plano atual não inclui execução autônoma.</p>
              <Link href="/plans" className="text-xs text-accent-light hover:underline">Ver planos</Link>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
