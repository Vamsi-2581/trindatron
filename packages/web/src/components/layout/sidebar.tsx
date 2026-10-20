'use client'

import { useState } from 'react'
import Link from 'next/link'
import { useRouter, usePathname } from 'next/navigation'
import {
  MessageSquare, FolderOpen, Bot, Code2, Briefcase, Activity,
  Settings, CreditCard, Brain, Plus, Trash2, LogOut, Lock,
  MousePointerClick, CalendarClock, Puzzle, ChevronDown,
} from 'lucide-react'
import { BrandLockup } from '@/components/brand/master'

/** Recent conversations shown before "Mostrar mais": enough to find today's work, never a wall. */
const RECENT_CONVERSATIONS = 8
import { useAppStore } from '@/store/useAppStore'
import { apiDeleteSession, apiLogout } from '@/api/client'

const PLAN_LABELS: Record<string, string> = {
  free: 'FREE', start: 'START', pro: 'PRO', max: 'MAX', team: 'TEAM',
}

// `built: false` means the screen itself is still a placeholder, so "EM BREVE"
// is the truth about it for everyone. Everything else here is a working screen
// and must never carry that badge — it used to, because the badge was the
// fallback branch of a check that only knew about CODE and WORK. A START user
// was told that Projetos and Agentes were coming soon while their own plan
// already granted both, which reads as a product that does not know what it
// ships.
//
// TRINDATRON is the product; these are things it does, not products of their
// own. "Desenvolvimento" and "Execução autônoma" now say where that work
// actually happens (a project, a conversation) instead of naming an internal
// component as if it were something to buy.
const WORKSPACE_NAV = [
  { href: '/projects', icon: FolderOpen, label: 'Projetos', built: true },
  { href: '/agents', icon: Bot, label: 'Agentes', built: true },
  { href: '/agent-instances', icon: Activity, label: 'Agentes Persistentes', built: true },
  { href: '/code', icon: Code2, label: 'Desenvolvimento', built: true },
  { href: '/work', icon: Briefcase, label: 'Execução autônoma', built: true },
]

// What TRINDATRON does for the person beyond a conversation. Each page is built and reads its real state
// from the Core (capability matrix, scheduled tasks, integrations); a plan that lacks one says so on the page.
const FEATURE_NAV = [
  { href: '/computer-use', icon: MousePointerClick, label: 'Computer Use' },
  { href: '/scheduled', icon: CalendarClock, label: 'Agendados' },
  { href: '/settings/plugins', icon: Puzzle, label: 'Plugins' },
]

export function Sidebar() {
  const router = useRouter()
  const pathname = usePathname()
  const { sessions, currentSessionId, removeSession, selectSession, user, clearAuth, entitlement } = useAppStore()
  const [showAll, setShowAll] = useState(false)
  const visibleSessions = showAll ? sessions : sessions.slice(0, RECENT_CONVERSATIONS)

  function isActive(href: string) {
    return pathname.startsWith(href)
  }

  async function deleteSession(e: React.MouseEvent, id: string) {
    e.preventDefault()
    e.stopPropagation()
    await apiDeleteSession(id).catch(() => null)
    removeSession(id)
    if (currentSessionId === id) router.push('/home')
  }

  async function logout() {
    await apiLogout()
    clearAuth()
    router.replace('/auth')
  }

  const planLabel = user ? (PLAN_LABELS[user.plan] ?? user.plan.toUpperCase()) : ''
  const initials = user?.email?.[0]?.toUpperCase() ?? 'U'

  return (
    <aside className="flex flex-col w-60 shrink-0 bg-surface-1 border-r border-border h-screen overflow-hidden">
      {/* Brand */}
      <div className="flex items-center px-5 py-3.5 border-b border-border">
        <BrandLockup size="sm" />
      </div>

      {/* New conversation */}
      <div className="px-3 pt-3 pb-1">
        <Link
          href="/home"
          className="flex items-center gap-2 w-full px-3 py-2 rounded-lg text-sm text-text-secondary hover:text-text-primary hover:bg-surface-2 transition-colors"
        >
          <Plus className="w-4 h-4 shrink-0" />
          <span>Nova conversa</span>
        </Link>
      </div>

      {/* Everything between "Nova conversa" and the account scrolls as one column, conversations first.
          The list used to be the only flexible part under four fixed blocks, and on a laptop screen it was
          squeezed to a few pixels: the conversations existed but could not be seen or reopened. */}
      <div className="flex-1 min-h-0 overflow-y-auto">
      <div className="px-3 pb-2">
        {sessions.length > 0 && (
          <>
            <p className="px-3 pt-3 pb-1.5 text-[10px] font-semibold text-text-muted uppercase tracking-wider">
              Conversas
            </p>
            {visibleSessions.map((session) => {
              const active = pathname === `/chat/${session.id}`
              return (
                <div key={session.id} className="group relative mb-0.5">
                  <Link
                    href={`/chat/${session.id}`}
                    onClick={() => selectSession(session.id)}
                    className={[
                      'flex items-center gap-2 pl-3 pr-8 py-2 rounded-lg text-sm transition-colors',
                      active
                        ? 'bg-accent/10 text-text-primary'
                        : 'text-text-secondary hover:text-text-primary hover:bg-surface-2',
                    ].join(' ')}
                  >
                    <MessageSquare className="w-3.5 h-3.5 shrink-0 opacity-50" />
                    <span className="truncate leading-none text-xs">
                      {session.title || 'Nova conversa'}
                    </span>
                  </Link>
                  <button
                    onClick={(e) => deleteSession(e, session.id)}
                    title="Excluir conversa"
                    className="absolute right-2 top-1/2 -translate-y-1/2 p-1 rounded text-text-muted opacity-0 group-hover:opacity-100 hover:text-error hover:bg-error/10 transition-all"
                  >
                    <Trash2 className="w-3 h-3" />
                  </button>
                </div>
              )
            })}
            {sessions.length > RECENT_CONVERSATIONS && (
              <button
                type="button"
                onClick={() => setShowAll((all) => !all)}
                className="flex items-center gap-2 w-full pl-3 py-1.5 rounded-lg text-xs text-text-muted hover:text-text-primary hover:bg-surface-2 transition-colors"
              >
                <ChevronDown className={`w-3.5 h-3.5 shrink-0 transition-transform ${showAll ? 'rotate-180' : ''}`} />
                {showAll ? 'Mostrar menos' : `Mostrar mais (${sessions.length - RECENT_CONVERSATIONS})`}
              </button>
            )}
          </>
        )}
      </div>

      {/* Feature nav */}
      <div className="px-3 py-2 border-t border-border">
        {FEATURE_NAV.map(({ href, icon: Icon, label }) => (
          <Link
            key={href}
            href={href}
            className={[
              'flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-colors mb-0.5',
              isActive(href)
                ? 'bg-surface-2 text-text-primary'
                : 'text-text-secondary hover:text-text-primary hover:bg-surface-2',
            ].join(' ')}
          >
            <Icon className="w-4 h-4 shrink-0" />
            <span className="flex-1 text-xs">{label}</span>
          </Link>
        ))}
      </div>

      {/* Workspace nav */}
      <div className="px-3 py-2 border-t border-border">
        <p className="px-3 pt-2 pb-1.5 text-[10px] font-semibold text-text-muted uppercase tracking-wider">
          Espaços
        </p>
        {WORKSPACE_NAV.map(({ href, icon: Icon, label, built }) => {
          // A plan gate outranks a build state: if the user cannot reach the
          // space at all, the lock is the useful thing to show. Only when
          // the space is reachable does "not built yet" become the message.
          const locked = entitlement
            ? (href === '/code' && !entitlement.features.supports_trinity_code) ||
              (href === '/work' && !entitlement.features.supports_trinity_work) ||
              (href === '/agents' && !entitlement.features.supports_agent_mode) ||
              (href === '/agent-instances' && !entitlement.features.supports_agent_mode)
            : false
          return (
            <Link
              key={href}
              href={href}
              className={[
                'flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-colors mb-0.5',
                isActive(href)
                  ? 'bg-surface-2 text-text-primary'
                  : 'text-text-secondary hover:text-text-primary hover:bg-surface-2',
              ].join(' ')}
            >
              <Icon className="w-4 h-4 shrink-0" />
              <span className="flex-1 text-xs">{label}</span>
              {locked ? (
                <Lock className="w-3 h-3 text-text-muted opacity-60 shrink-0" />
              ) : !built ? (
                <span className="text-[9px] font-medium text-text-muted bg-surface-3 px-1.5 py-0.5 rounded-full tracking-wide opacity-70">
                  EM BREVE
                </span>
              ) : null}
            </Link>
          )
        })}
      </div>

      {/* Bottom nav */}
      <div className="px-3 pt-2 pb-1 border-t border-border space-y-0.5">
        <Link
          href="/memory"
          className={[
            'flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-colors',
            isActive('/memory')
              ? 'bg-surface-2 text-text-primary'
              : 'text-text-secondary hover:text-text-primary hover:bg-surface-2',
          ].join(' ')}
        >
          <Brain className="w-4 h-4 shrink-0" />
          <span className="text-xs">Memórias</span>
        </Link>
        <Link
          href="/plans"
          className={[
            'flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-colors',
            isActive('/plans')
              ? 'bg-surface-2 text-text-primary'
              : 'text-text-secondary hover:text-text-primary hover:bg-surface-2',
          ].join(' ')}
        >
          <CreditCard className="w-4 h-4 shrink-0" />
          <span className="text-xs">Planos</span>
        </Link>
        <Link
          href="/settings"
          className={[
            'flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-colors',
            isActive('/settings')
              ? 'bg-surface-2 text-text-primary'
              : 'text-text-secondary hover:text-text-primary hover:bg-surface-2',
          ].join(' ')}
        >
          <Settings className="w-4 h-4 shrink-0" />
          <span className="text-xs">Configurações</span>
        </Link>
      </div>
      </div>

      {/* User footer */}
      <div className="px-3 py-3 border-t border-border">
        <div className="flex items-center gap-2.5 px-2">
          <Link
            href="/account"
            className="w-7 h-7 rounded-full bg-accent/20 flex items-center justify-center shrink-0 hover:bg-accent/30 transition-colors"
            title="Minha conta"
          >
            <span className="text-xs font-semibold text-accent-light">{initials}</span>
          </Link>
          <div className="flex-1 min-w-0">
            <p className="text-xs text-text-primary truncate leading-none">{user?.email ?? '…'}</p>
            <p className="text-[10px] text-text-muted mt-0.5 leading-none">{planLabel}</p>
          </div>
          <button
            onClick={logout}
            title="Sair"
            className="p-1.5 rounded-lg text-text-muted hover:text-error hover:bg-error/10 transition-colors"
          >
            <LogOut className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
    </aside>
  )
}
