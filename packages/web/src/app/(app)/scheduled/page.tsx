'use client'

import { ScheduledTasks } from '@/components/scheduled/scheduled-tasks'

export default function ScheduledPage() {
  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-2xl mx-auto px-6 py-10">
        <h1 className="text-xl font-semibold text-text-primary">Agendados</h1>
        <p className="text-sm text-text-muted mt-1 mb-6">Instruções que o TRINDATRON cumpre sozinho, no horário que você escolher.</p>
        <ScheduledTasks />
      </div>
    </div>
  )
}
