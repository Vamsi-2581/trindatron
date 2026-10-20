'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import type { ReactNode } from 'react'
import { SETTINGS_GROUPS } from '@/components/settings/nav'

export default function SettingsLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname()

  return (
    <div className="flex-1 flex overflow-hidden">
      <nav className="w-56 shrink-0 border-r border-border overflow-y-auto px-3 py-6" aria-label="Configurações">
        <p className="px-3 pb-4 text-sm font-semibold text-text-primary">Configurações</p>
        {SETTINGS_GROUPS.map((group) => (
          <div key={group.label} className="mb-5">
            <p className="px-3 pb-1.5 text-[10px] font-semibold text-text-muted uppercase tracking-wider">{group.label}</p>
            {group.sections.map((section) => {
              const href = `/settings/${section.id}`
              const active = pathname === href
              return (
                <Link
                  key={section.id}
                  href={href}
                  className={[
                    'block px-3 py-1.5 rounded-lg text-xs transition-colors mb-0.5',
                    active ? 'bg-surface-2 text-text-primary' : 'text-text-secondary hover:text-text-primary hover:bg-surface-2',
                  ].join(' ')}
                >
                  {section.label}
                </Link>
              )
            })}
          </div>
        ))}
      </nav>
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-2xl mx-auto px-6 py-10">{children}</div>
      </div>
    </div>
  )
}
