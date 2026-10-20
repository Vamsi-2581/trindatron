import { router } from 'expo-router'
import { useState } from 'react'
import { StyleSheet, View } from 'react-native'

import {
  AppText,
  Badge,
  Button,
  EmptyState,
  ErrorState,
  ListRow,
  Notice,
  Screen,
  Section,
  SkeletonRows,
  Surface,
} from '@/components/ui'
import { INSTANCE_STATUS_TONE, instanceStatusKey, JOB_STATUS_TONE, jobStatusKey } from '@/features/agents/status'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useT } from '@/i18n'
import {
  listAgentDefinitions,
  listAgentInstances,
  listExecutionJobs,
  pauseAgentInstance,
  resumeAgentInstance,
  runAgentInstance,
} from '@/services/api/agents'
import { userMessage } from '@/services/api/errors'
import { createSession } from '@/services/api/sessions'
import type { AgentInstance } from '@/services/api/types'
import { space } from '@/theme'
import { formatRelativeTime } from '@/utils/dates'

/** Agents run inside TRINITY CORE. This screen observes and steers them — it never executes agent logic. */
export default function AgentsScreen() {
  const t = useT()
  const definitions = useAsyncResource((signal) => listAgentDefinitions(signal), [], { refetchOnFocus: true })
  const instances = useAsyncResource((signal) => listAgentInstances(signal), [], { refetchOnFocus: true })
  const jobs = useAsyncResource((signal) => listExecutionJobs(undefined, signal), [], { refetchOnFocus: true })
  const [busy, setBusy] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  async function chatWith(agentId: string) {
    setBusy(agentId)
    setNotice(null)
    try {
      const session = await createSession({ agentDefinitionId: agentId })
      router.push({ pathname: '/chat/[sessionId]', params: { sessionId: session.id } })
    } catch (cause) {
      setNotice(userMessage(cause))
    } finally {
      setBusy(null)
    }
  }

  async function act(instance: AgentInstance, action: 'pause' | 'resume' | 'run') {
    setBusy(`${instance.id}:${action}`)
    setNotice(null)
    try {
      if (action === 'run') {
        const job = await runAgentInstance(instance.id)
        void jobs.refresh()
        router.push({ pathname: '/job/[jobId]', params: { jobId: job.id } })
        return
      }
      const updated = action === 'pause' ? await pauseAgentInstance(instance.id) : await resumeAgentInstance(instance.id)
      instances.setData((current) =>
        current ? { ...current, instances: current.instances.map((item) => (item.id === updated.id ? updated : item)) } : current,
      )
    } catch (cause) {
      setNotice(userMessage(cause))
    } finally {
      setBusy(null)
    }
  }

  const recentJobs = [...(jobs.data?.jobs ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 20)
  const activeAgents = definitions.data?.agents.filter((agent) => !agent.is_archived) ?? []
  // Nothing anywhere: one empty state for the page. Three stacked ones, each with a sentence as its bold title, read
  // as a broken screen on the phone.
  const nothingYet =
    Boolean(definitions.data && instances.data && jobs.data) &&
    activeAgents.length === 0 &&
    (instances.data?.instances.length ?? 0) === 0 &&
    recentJobs.length === 0

  return (
    <Screen
      refreshing={definitions.refreshing || instances.refreshing || jobs.refreshing}
      onRefresh={() => {
        void definitions.refresh()
        void instances.refresh()
        void jobs.refresh()
      }}
    >
      <AppText tone="secondary">{t('agents.intro')}</AppText>
      {notice ? <Notice tone="error">{notice}</Notice> : null}

      {nothingYet ? (
        <EmptyState icon="sparkles-outline" title={t('agents.emptyTitle')} body={t('agents.emptyBody')} />
      ) : (
        <>
          <Section title={t('agents.definitions')}>
            {definitions.loading && !definitions.data ? (
              <SkeletonRows count={2} icon />
            ) : definitions.error && !definitions.data ? (
              <ErrorState message={definitions.error.message} onRetry={definitions.refresh} />
            ) : activeAgents.length === 0 ? (
              <SectionNote text={t('agents.noDefinitions')} />
            ) : (
              <Surface>
                {activeAgents.map((agent) => (
                    <ListRow
                      key={agent.id}
                      icon="sparkles-outline"
                      title={`${agent.avatar_emoji ? `${agent.avatar_emoji} ` : ''}${agent.name}`}
                      subtitle={agent.description}
                      trailing={<Button label={t('agents.chat')} variant="ghost" loading={busy === agent.id} onPress={() => chatWith(agent.id)} />}
                    />
                  ))}
              </Surface>
            )}
          </Section>

          <Section title={t('agents.persistent')}>
            {instances.loading && !instances.data ? (
              <SkeletonRows count={2} icon />
            ) : instances.error && !instances.data ? (
              <ErrorState message={instances.error.message} onRetry={instances.refresh} />
            ) : (instances.data?.instances.length ?? 0) === 0 ? (
              <SectionNote text={t('agents.noPersistent')} />
            ) : (
              instances.data?.instances.map((instance) => (
                <Surface key={instance.id} style={styles.instance}>
                  <View style={styles.instanceHeader}>
                    <AppText variant="bodyStrong" style={styles.flex} numberOfLines={1}>
                      {instance.name}
                    </AppText>
                    <Badge label={t(instanceStatusKey(instance.status))} tone={INSTANCE_STATUS_TONE[instance.status]} />
                  </View>
                  <AppText variant="caption" tone="muted">
                    {instance.last_run_at ? t('agents.lastRun', { when: formatRelativeTime(instance.last_run_at) }) : t('agents.neverRan')}
                    {instance.current_execution_job_status
                      ? ` · ${t('agents.current', { status: t(jobStatusKey(instance.current_execution_job_status)) })}`
                      : ''}
                  </AppText>
                  {instance.last_error ? <Notice tone="error">{t('agents.instanceError')}</Notice> : null}
                  <View style={styles.actions}>
                    {instance.status === 'active' ? (
                      <Button label={t('agents.pause')} variant="secondary" icon="pause" loading={busy === `${instance.id}:pause`} onPress={() => act(instance, 'pause')} />
                    ) : (
                      <Button label={t('agents.resume')} variant="secondary" icon="play" loading={busy === `${instance.id}:resume`} onPress={() => act(instance, 'resume')} />
                    )}
                    <Button label={t('agents.runNow')} icon="flash-outline" loading={busy === `${instance.id}:run`} onPress={() => act(instance, 'run')} />
                  </View>
                </Surface>
              ))
            )}
          </Section>

          <Section title={t('agents.recentRuns')}>
            {jobs.loading && !jobs.data ? (
              <SkeletonRows count={2} icon />
            ) : jobs.error && !jobs.data ? (
              <ErrorState message={jobs.error.message} onRetry={jobs.refresh} />
            ) : recentJobs.length === 0 ? (
              <SectionNote text={t('agents.noRuns')} />
            ) : (
              <Surface>
                {recentJobs.map((job) => (
                  <ListRow
                    key={job.id}
                    icon="pulse-outline"
                    title={t('agents.task')}
                    subtitle={formatRelativeTime(job.created_at)}
                    trailing={<Badge label={t(jobStatusKey(job.status))} tone={JOB_STATUS_TONE[job.status]} />}
                    onPress={() => router.push({ pathname: '/job/[jobId]', params: { jobId: job.id } })}
                  />
                ))}
              </Surface>
            )}
          </Section>
        </>
      )}
    </Screen>
  )
}

/** An empty section among others that are not: a quiet line, not a second empty state. */
function SectionNote({ text }: { text: string }) {
  return (
    <AppText variant="caption" tone="muted">
      {text}
    </AppText>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  instance: { padding: space.lg, gap: space.sm },
  instanceHeader: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  actions: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm },
})
