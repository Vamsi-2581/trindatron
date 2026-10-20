'use client'

/**
 * Code workspaces exist and work: the Core creates them, runs commands in them
 * and streams the work back (/workspaces/code), and the screen that drives one
 * is /code/[id]. What never existed is a list: the Core has no "my workspaces"
 * endpoint, because a workspace belongs to a project and is opened from there.
 *
 * This page used to fill that gap with a grid of features and an "Em breve"
 * badge, which described a product that was already shipped and sent people
 * looking for it in the wrong place.
 */
import Link from 'next/link'
import { Code2, Lock } from 'lucide-react'
import { useAppStore } from '@/store/useAppStore'

export default function CodePage() {
  const { entitlement } = useAppStore()
  const included = !entitlement || entitlement.features.supports_trinity_code

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-2xl mx-auto px-6 py-12">
        <div className="w-11 h-11 rounded-xl bg-accent/10 border border-accent/20 flex items-center justify-center mb-5">
          <Code2 className="w-5 h-5 text-accent-light" />
        </div>
        <h1 className="text-xl font-semibold text-text-primary">Desenvolvimento</h1>

        {included ? (
          <>
            <p className="text-sm text-text-muted mt-2 leading-relaxed">
              O TRINDATRON lê e edita os arquivos do seu projeto, roda comandos num ambiente isolado e mostra cada
              passo — e cada ação que altera algo espera a sua aprovação.
            </p>
            <p className="text-sm text-text-secondary mt-6 leading-relaxed">
              Um espaço de desenvolvimento pertence a um projeto: abra o projeto e ele começa ali, junto dos arquivos
              e do contexto daquele trabalho.
            </p>
            <Link
              href="/projects"
              className="inline-flex items-center gap-2 mt-6 px-4 py-2 rounded-xl bg-accent/10 border border-accent/30 text-sm text-accent-light hover:bg-accent/15 transition-colors"
            >
              Abrir meus projetos
            </Link>
          </>
        ) : (
          <div className="mt-6 flex items-start gap-3 px-4 py-4 rounded-xl bg-surface-1 border border-border">
            <Lock className="w-4 h-4 text-text-muted shrink-0 mt-0.5" />
            <div>
              <p className="text-sm text-text-primary">Seu plano atual não inclui desenvolvimento.</p>
              <Link href="/plans" className="text-xs text-accent-light hover:underline">Ver planos</Link>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
