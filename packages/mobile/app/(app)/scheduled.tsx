import { Stack } from 'expo-router'
import { useState } from 'react'
import { StyleSheet, View } from 'react-native'

import {
  AppText,
  Badge,
  BottomSheet,
  Button,
  EmptyState,
  ErrorState,
  IconButton,
  Notice,
  Screen,
  SkeletonRows,
  Surface,
  TextField,
} from '@/components/ui'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useT } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import {
  createScheduledTask,
  deleteScheduledTask,
  listScheduledTasks,
  runScheduledTask,
  updateScheduledTask,
  type Recurrence,
  type RecurrenceKind,
  type ScheduledTask,
} from '@/services/api/scheduled'
import { space } from '@/theme'
import { confirmAction } from '@/utils/confirm'
import { formatDateTime } from '@/utils/dates'

const KINDS: RecurrenceKind[] = ['hourly', 'daily', 'weekly']
const HOURS = ['06:00', '07:00', '08:00', '09:00', '12:00', '18:00', '20:00', '22:00']

/**
 * Agendados: an instruction TRINDATRON carries out on its own, at a time the
 * person chooses.
 *
 * Nothing here decides anything: the Core owns the schedule, the plan gate and
 * the limit, and every refusal it sends (not in your plan, too many active,
 * one already running) is shown in its own words rather than flattened into
 * "something went wrong". The device's time zone travels with the request so
 * "09:00" means nine in the morning where the person is.
 */
export default function ScheduledScreen() {
  const t = useT()
  const tasks = useAsyncResource((signal) => listScheduledTasks(signal), [], { refetchOnFocus: true })
  const [composing, setComposing] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const list = tasks.data?.tasks ?? []

  async function act(id: string, action: () => Promise<unknown>) {
    setBusy(id)
    setError(null)
    try {
      await action()
      await tasks.refresh()
    } catch (cause) {
      setError(userMessage(cause))
    } finally {
      setBusy(null)
    }
  }

  async function remove(task: ScheduledTask) {
    const confirmed = await confirmAction({
      title: t('scheduled.deleteTitle'),
      message: t('scheduled.deleteMessage', { title: task.title }),
      confirmLabel: t('common.delete'),
      destructive: true,
    })
    if (confirmed) await act(task.id, () => deleteScheduledTask(task.id))
  }

  return (
    <Screen edges={['bottom', 'left', 'right']} refreshing={tasks.refreshing} onRefresh={tasks.refresh}>
      <Stack.Screen
        options={{ headerRight: () => <IconButton icon="add" tone="accent" label={t('scheduled.new')} onPress={() => setComposing(true)} /> }}
      />
      {error ? <Notice tone="error">{error}</Notice> : null}

      {tasks.loading && !tasks.data ? (
        <SkeletonRows count={3} />
      ) : tasks.error && !tasks.data ? (
        <ErrorState message={tasks.error.message} onRetry={tasks.refresh} />
      ) : (
        <>
          {list.length === 0 ? (
            <EmptyState
              icon="alarm-outline"
              title={t('scheduled.emptyTitle')}
              body={t('scheduled.emptyBody')}
              action={<Button label={t('scheduled.new')} icon="add" onPress={() => setComposing(true)} />}
            />
          ) : (
            <View style={styles.list}>
              {list.map((task) => (
                <Surface key={task.id} style={styles.card}>
                  <View style={styles.header}>
                    <AppText variant="bodyStrong" style={styles.flex} numberOfLines={2}>
                      {task.title}
                    </AppText>
                    <Badge
                      label={task.is_active ? t('scheduled.active') : t('scheduled.paused')}
                      tone={task.is_active ? 'accent' : 'neutral'}
                    />
                  </View>

                  <AppText variant="caption" tone="secondary" numberOfLines={3}>
                    {task.instruction}
                  </AppText>

                  <AppText variant="caption" tone="muted">
                    {describe(task.recurrence, t)}
                    {task.recurrence.timezone ? ` · ${task.recurrence.timezone}` : ''}
                  </AppText>
                  <AppText variant="caption" tone="muted">
                    {task.is_active && task.next_run_at
                      ? t('scheduled.nextRun', { when: formatDateTime(task.next_run_at) })
                      : t('scheduled.noNextRun')}
                  </AppText>

                  {task.last_run ? (
                    <View style={styles.lastRun}>
                      <Badge
                        label={t(`scheduled.runStatus.${task.last_run.status}`) === `scheduled.runStatus.${task.last_run.status}`
                          ? task.last_run.status
                          : t(`scheduled.runStatus.${task.last_run.status}`)}
                        tone={task.last_run.status === 'completed' ? 'success' : task.last_run.status === 'failed' ? 'error' : 'neutral'}
                      />
                      {task.last_run.excerpt ? (
                        <AppText variant="caption" tone="secondary" numberOfLines={4}>
                          {task.last_run.excerpt}
                        </AppText>
                      ) : null}
                    </View>
                  ) : null}

                  <View style={styles.actions}>
                    <Button
                      label={t('scheduled.runNow')}
                      variant="secondary"
                      icon="play-outline"
                      loading={busy === task.id}
                      disabled={busy !== null}
                      onPress={() => void act(task.id, () => runScheduledTask(task.id))}
                    />
                    <Button
                      label={task.is_active ? t('scheduled.pause') : t('scheduled.resume')}
                      variant="ghost"
                      icon={task.is_active ? 'pause-outline' : 'play-outline'}
                      disabled={busy !== null}
                      onPress={() => void act(task.id, () => updateScheduledTask(task.id, { is_active: !task.is_active }))}
                    />
                    <IconButton
                      icon="trash-outline"
                      tone="danger"
                      label={t('scheduled.deleteA11y', { title: task.title })}
                      disabled={busy !== null}
                      onPress={() => void remove(task)}
                    />
                  </View>
                </Surface>
              ))}
            </View>
          )}
        </>
      )}

      <NewTaskSheet
        visible={composing}
        onClose={() => setComposing(false)}
        onCreated={() => {
          setComposing(false)
          void tasks.refresh()
        }}
        onError={setError}
      />
    </Screen>
  )
}

function describe(recurrence: Recurrence, t: (key: string, params?: Record<string, string | number>) => string): string {
  if (recurrence.kind === 'hourly') return t('scheduled.everyHour')
  if (recurrence.kind === 'daily') return t('scheduled.everyDay', { time: recurrence.time ?? '' })
  return t('scheduled.everyWeek', {
    weekday: t(`scheduled.weekday.${recurrence.weekday ?? 0}`),
    time: recurrence.time ?? '',
  })
}

function NewTaskSheet({
  visible,
  onClose,
  onCreated,
  onError,
}: {
  visible: boolean
  onClose: () => void
  onCreated: () => void
  onError: (message: string) => void
}) {
  const t = useT()
  const [title, setTitle] = useState('')
  const [instruction, setInstruction] = useState('')
  const [kind, setKind] = useState<RecurrenceKind>('daily')
  const [time, setTime] = useState('09:00')
  const [weekday, setWeekday] = useState(0)
  const [saving, setSaving] = useState(false)

  async function create() {
    if (!title.trim() || !instruction.trim() || saving) return
    setSaving(true)
    // The device's zone, so a time means the same hour the person sees on their own clock.
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone
    const recurrence: Recurrence =
      kind === 'hourly'
        ? { kind, timezone }
        : kind === 'daily'
          ? { kind, time, timezone }
          : { kind, time, weekday, timezone }
    try {
      await createScheduledTask({ title: title.trim(), instruction: instruction.trim(), recurrence })
      setTitle('')
      setInstruction('')
      onCreated()
    } catch (cause) {
      onError(userMessage(cause))
      onClose()
    } finally {
      setSaving(false)
    }
  }

  return (
    <BottomSheet visible={visible} onClose={onClose} title={t('scheduled.new')}>
      <View style={styles.form}>
        <TextField label={t('scheduled.title')} value={title} onChangeText={setTitle} maxLength={120} />
        <TextField
          label={t('scheduled.instruction')}
          value={instruction}
          onChangeText={setInstruction}
          multiline
          maxLength={4000}
          hint={t('scheduled.instructionHint')}
        />

        <AppText variant="label" tone="muted">
          {t('scheduled.repeat')}
        </AppText>
        <View style={styles.choices}>
          {KINDS.map((option) => (
            <Button
              key={option}
              label={t(`scheduled.kind.${option}`)}
              variant={kind === option ? 'primary' : 'secondary'}
              onPress={() => setKind(option)}
            />
          ))}
        </View>

        {kind === 'weekly' ? (
          <View style={styles.choices}>
            {[0, 1, 2, 3, 4, 5, 6].map((day) => (
              <Button
                key={day}
                label={t(`scheduled.weekdayShort.${day}`)}
                variant={weekday === day ? 'primary' : 'secondary'}
                onPress={() => setWeekday(day)}
              />
            ))}
          </View>
        ) : null}

        {kind !== 'hourly' ? (
          <View style={styles.choices}>
            {HOURS.map((hour) => (
              <Button key={hour} label={hour} variant={time === hour ? 'primary' : 'secondary'} onPress={() => setTime(hour)} />
            ))}
          </View>
        ) : null}

        <Button
          label={t('scheduled.create')}
          icon="alarm-outline"
          fullWidth
          loading={saving}
          disabled={!title.trim() || !instruction.trim()}
          onPress={() => void create()}
        />
        <AppText variant="caption" tone="muted">
          {t('scheduled.researchNote')}
        </AppText>
      </View>
    </BottomSheet>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  list: { gap: space.md },
  card: { padding: space.lg, gap: space.xs },
  header: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  lastRun: { gap: space.xs, marginTop: space.xs },
  actions: { flexDirection: 'row', alignItems: 'center', gap: space.sm, flexWrap: 'wrap', marginTop: space.xs },
  form: { padding: space.md, gap: space.sm },
  choices: { flexDirection: 'row', flexWrap: 'wrap', gap: space.xs },
})
