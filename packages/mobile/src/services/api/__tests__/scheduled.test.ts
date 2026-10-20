import { apiRequest } from '../client'
import {
  createScheduledTask,
  deleteScheduledTask,
  listScheduledTasks,
  runScheduledTask,
  updateScheduledTask,
} from '../scheduled'

jest.mock('../client', () => ({ apiRequest: jest.fn() }))

const request = apiRequest as jest.Mock

describe('scheduled tasks API', () => {
  beforeEach(() => request.mockReset().mockResolvedValue({}))

  it('uses the Core scheduler, with the recurrence and time zone as sent', async () => {
    await listScheduledTasks()
    expect(request).toHaveBeenCalledWith('/scheduled-tasks', { signal: undefined })

    await createScheduledTask({
      title: 'Resumo da manhã',
      instruction: 'Resuma minhas prioridades do dia.',
      recurrence: { kind: 'daily', time: '09:00', timezone: 'America/Cuiaba' },
    })
    expect(request).toHaveBeenLastCalledWith('/scheduled-tasks', {
      method: 'POST',
      body: {
        title: 'Resumo da manhã',
        instruction: 'Resuma minhas prioridades do dia.',
        recurrence: { kind: 'daily', time: '09:00', timezone: 'America/Cuiaba' },
      },
    })
  })

  it('edits, pauses, deletes and runs one task by its own id', async () => {
    await updateScheduledTask('t 1', { is_active: false })
    expect(request).toHaveBeenLastCalledWith('/scheduled-tasks/t%201', { method: 'PATCH', body: { is_active: false } })

    await updateScheduledTask('t1', { recurrence: { kind: 'weekly', time: '08:30', weekday: 0 } })
    expect(request).toHaveBeenLastCalledWith('/scheduled-tasks/t1', {
      method: 'PATCH',
      body: { recurrence: { kind: 'weekly', time: '08:30', weekday: 0 } },
    })

    await deleteScheduledTask('t1')
    expect(request).toHaveBeenLastCalledWith('/scheduled-tasks/t1', { method: 'DELETE' })

    await runScheduledTask('t1')
    expect(request).toHaveBeenLastCalledWith('/scheduled-tasks/t1/run', { method: 'POST' })
  })

  it('decides nothing about plans or limits on the device', async () => {
    // plan_not_included, limit_reached and job_in_progress are the Core's answers. The client
    // does not predict them, soften them or retry around them — it passes them straight up.
    const refusal = Object.assign(new Error('plan_not_included'), { code: 'plan_not_included' })
    request.mockRejectedValueOnce(refusal)
    await expect(
      createScheduledTask({ title: 'x', instruction: 'y', recurrence: { kind: 'hourly' } }),
    ).rejects.toBe(refusal)

    const busy = Object.assign(new Error('job_in_progress'), { code: 'job_in_progress' })
    request.mockRejectedValueOnce(busy)
    await expect(runScheduledTask('t1')).rejects.toBe(busy)
  })
})
