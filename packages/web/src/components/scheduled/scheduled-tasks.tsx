'use client'

/**
 * Scheduled tasks ("Agendados"). Created, paused, run and deleted through the
 * Core (/scheduled-tasks), which fires them with the existing schedule engine
 * and runs each through the execution runtime. What a plan does not include,
 * the Core refuses, and the refusal is shown as it came.
 */
import { useCallback, useEffect, useState } from 'react'
import {
  apiCreateScheduledTask,
  apiDeleteScheduledTask,
  apiListScheduledTasks,
  apiRunScheduledTask,
  apiUpdateScheduledTask,
  type RecurrenceKind,
  type ScheduledTask,
} from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'

const WEEKDAYS = ['Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado', 'Domingo']

function describe(task: ScheduledTask): string {
  const { kind, time, weekday } = task.recurrence
  if (kind === 'hourly') return 'A cada hora'
  if (kind === 'daily') return `Todos os dias às ${time}`
  return `Toda ${WEEKDAYS[weekday ?? 0].toLowerCase()} às ${time}`
}

function when(value: string | null): string {
  if (!value) return '—'
  return new Date(value).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' })
}

const RUN_STATUS: Record<string, string> = { queued: 'Na fila', running: 'Executando', completed: 'Concluída', failed: 'Falhou', cancelled: 'Cancelada' }

export function ScheduledTasks() {
  const [tasks, setTasks] = useState<ScheduledTask[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [title, setTitle] = useState('')
  const [instruction, setInstruction] = useState('')
  const [kind, setKind] = useState<RecurrenceKind>('daily')
  const [time, setTime] = useState('09:00')
  const [weekday, setWeekday] = useState(0)
  const [saving, setSaving] = useState(false)

  const load = useCallback(() => {
    apiListScheduledTasks().then(setTasks).catch((err: unknown) => setError(err instanceof Error ? err.message : 'Não foi possível carregar.'))
  }, [])

  useEffect(() => { load() }, [load])

  async function act(action: () => Promise<unknown>) {
    setError(null)
    try {
      await action()
      load()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Não foi possível concluir.')
    }
  }

  async function create() {
    if (!title.trim() || !instruction.trim()) return
    setSaving(true)
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone
    await act(() => apiCreateScheduledTask({
      title, instruction,
      recurrence: kind === 'hourly' ? { kind, timezone } : kind === 'daily' ? { kind, time, timezone } : { kind, time, weekday, timezone },
    }).then(() => { setTitle(''); setInstruction('') }))
    setSaving(false)
  }

  return (
    <div className="space-y-4">
      <Card>
        <Card.Header><p className="text-sm font-medium text-text-primary">Nova tarefa agendada</p></Card.Header>
        <Card.Body>
          <div className="space-y-3">
            <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={120} placeholder="Título (ex.: Resumo da manhã)"
              className="w-full bg-surface-2 border border-border rounded-lg px-3 py-2 text-sm text-text-primary" />
            <textarea value={instruction} onChange={(e) => setInstruction(e.target.value)} maxLength={4000} rows={3}
              placeholder="O que o TRINDATRON deve fazer (ex.: Faça um resumo das tendências de IA desta semana em 5 tópicos)"
              className="w-full bg-surface-2 border border-border rounded-lg px-3 py-2 text-sm text-text-primary" />
            <div className="flex flex-wrap gap-2 items-center">
              <select value={kind} onChange={(e) => setKind(e.target.value as RecurrenceKind)} className="bg-surface-2 border border-border rounded-lg px-2 py-1.5 text-xs text-text-primary">
                <option value="hourly">A cada hora</option>
                <option value="daily">Todo dia</option>
                <option value="weekly">Toda semana</option>
              </select>
              {kind === 'weekly' ? (
                <select value={weekday} onChange={(e) => setWeekday(Number(e.target.value))} className="bg-surface-2 border border-border rounded-lg px-2 py-1.5 text-xs text-text-primary">
                  {WEEKDAYS.map((day, index) => <option key={day} value={index}>{day}</option>)}
                </select>
              ) : null}
              {kind !== 'hourly' ? (
                <input type="time" value={time} onChange={(e) => setTime(e.target.value)} className="bg-surface-2 border border-border rounded-lg px-2 py-1.5 text-xs text-text-primary" />
              ) : null}
              <Button size="sm" loading={saving} disabled={!title.trim() || !instruction.trim()} onClick={() => void create()}>Agendar</Button>
            </div>
            <p className="text-xs text-text-muted">
              A tarefa pesquisa, escreve e resume no horário marcado. Ações em sites ou arquivos continuam acontecendo em conversas, com a sua aprovação.
            </p>
          </div>
        </Card.Body>
      </Card>

      {error ? <p className="text-xs text-error">{error}</p> : null}

      {tasks === null ? <p className="text-xs text-text-muted">Carregando…</p> : tasks.length === 0 ? (
        <p className="text-xs text-text-muted">Nenhuma tarefa agendada ainda.</p>
      ) : tasks.map((task) => (
        <Card key={task.id}>
          <Card.Body>
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-sm font-medium text-text-primary">{task.title}</p>
                <p className="text-xs text-text-muted">{describe(task)} · próxima: {task.is_active ? when(task.next_run_at) : 'pausada'}</p>
                <p className="text-xs text-text-secondary mt-1 line-clamp-2">{task.instruction}</p>
                {task.last_run ? (
                  <div className="mt-2 text-xs">
                    <Badge variant={task.last_run.status === 'completed' ? 'success' : task.last_run.status === 'failed' ? 'error' : 'default'}>
                      {RUN_STATUS[task.last_run.status] ?? task.last_run.status}
                    </Badge>
                    {task.last_run.excerpt ? <p className="text-text-secondary mt-1 whitespace-pre-line line-clamp-4">{task.last_run.excerpt}</p> : null}
                  </div>
                ) : null}
              </div>
              <div className="flex flex-col gap-1.5 shrink-0">
                <Button size="sm" variant="secondary" onClick={() => void act(() => apiRunScheduledTask(task.id))}>Executar agora</Button>
                <Button size="sm" variant="ghost" onClick={() => void act(() => apiUpdateScheduledTask(task.id, { is_active: !task.is_active }))}>
                  {task.is_active ? 'Pausar' : 'Retomar'}
                </Button>
                <Button size="sm" variant="destructive" onClick={() => { if (window.confirm('Excluir esta tarefa?')) void act(() => apiDeleteScheduledTask(task.id)) }}>Excluir</Button>
              </div>
            </div>
          </Card.Body>
        </Card>
      ))}
    </div>
  )
}
