import { useLocalSearchParams } from 'expo-router'
import { useEffect, useState } from 'react'
import { StyleSheet, View } from 'react-native'

import { AppText, Badge, Button, ErrorState, LoadingState, Notice, Screen, Section, Surface } from '@/components/ui'
import { jobResultText, JOB_STATUS_TONE, jobStatusKey } from '@/features/agents/status'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useT } from '@/i18n'
import { cancelExecutionJob, getExecutionJob, isTerminalJobStatus } from '@/services/api/agents'
import { userMessage } from '@/services/api/errors'
import { safeUserMessage } from '@/services/api/redact'
import { space } from '@/theme'
import { confirmAction } from '@/utils/confirm'
import { formatDateTime } from '@/utils/dates'
import { formatBytes } from '@/utils/format'

const POLL_MS = 3000

export default function JobScreen() {
  const t = useT()
  const { jobId } = useLocalSearchParams<{ jobId: string }>()
  const job = useAsyncResource((signal) => getExecutionJob(jobId, signal), [jobId])
  const [cancelling, setCancelling] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const { data, refresh } = job

  useEffect(() => {
    if (!data || isTerminalJobStatus(data.status)) return
    const timer = setTimeout(() => void refresh(), POLL_MS)
    return () => clearTimeout(timer)
  }, [data, refresh])

  if (job.loading && !data) {
    return (
      <Screen edges={['bottom', 'left', 'right']}>
        <LoadingState />
      </Screen>
    )
  }
  if (!data) {
    return (
      <Screen edges={['bottom', 'left', 'right']}>
        <ErrorState message={job.error?.message ?? t('job.notFound')} onRetry={refresh} />
      </Screen>
    )
  }

  const current = data
  const terminal = isTerminalJobStatus(current.status)
  const resultText = jobResultText(current.result)

  async function cancel() {
    const confirmed = await confirmAction({
      title: t('job.cancelTitle'),
      message: t('job.cancelMessage'),
      confirmLabel: t('job.cancel'),
      destructive: true,
    })
    if (!confirmed) return
    setCancelling(true)
    setNotice(null)
    try {
      job.setData(await cancelExecutionJob(current.id))
    } catch (cause) {
      setNotice(userMessage(cause))
    } finally {
      setCancelling(false)
    }
  }

  return (
    <Screen edges={['bottom', 'left', 'right']} refreshing={job.refreshing} onRefresh={refresh}>
      <View style={styles.header}>
        <AppText variant="title" accessibilityRole="header">
          {t('agents.task')}
        </AppText>
        <View style={styles.statusRow} accessibilityLiveRegion="polite">
          <Badge label={t(jobStatusKey(current.status))} tone={JOB_STATUS_TONE[current.status]} />
          {!terminal ? (
            <AppText variant="caption" tone="muted">
              {t('job.autoRefresh')}
            </AppText>
          ) : null}
        </View>
      </View>

      {notice ? <Notice tone="error">{notice}</Notice> : null}
      {current.error ? <Notice tone="error">{safeUserMessage(current.error, t('job.failed'))}</Notice> : null}

      <Section title={t('common.details')}>
        <Surface style={styles.details}>
          <Detail label={t('job.created')} value={formatDateTime(current.created_at)} />
          <Detail label={t('job.started')} value={current.started_at ? formatDateTime(current.started_at) : '—'} />
          <Detail label={t('job.completed')} value={current.completed_at ? formatDateTime(current.completed_at) : '—'} />
          <Detail label={t('job.attempt')} value={t('job.attemptValue', { current: current.attempt_number, total: current.max_retries + 1 })} />
        </Surface>
      </Section>

      {resultText ? (
        <Section title={t('job.result')}>
          <Surface style={styles.details}>
            <AppText selectable>{resultText}</AppText>
          </Surface>
        </Section>
      ) : null}

      {current.artifacts.length > 0 ? (
        <Section title={t('job.artifacts')}>
          <Surface style={styles.details}>
            {current.artifacts.map((artifact) => (
              <Detail key={artifact.id} label={artifact.filename} value={formatBytes(artifact.size_bytes)} />
            ))}
          </Surface>
          <AppText variant="caption" tone="muted">
            {t('job.artifactsUnavailable')}
          </AppText>
        </Section>
      ) : null}

      {!terminal ? <Button label={t('job.cancel')} variant="danger" icon="stop-circle-outline" loading={cancelling} onPress={cancel} /> : null}
    </Screen>
  )
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.detail}>
      <AppText variant="caption" tone="secondary" style={styles.flex} numberOfLines={1}>
        {label}
      </AppText>
      <AppText variant="caption">{value}</AppText>
    </View>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  header: { gap: space.sm },
  statusRow: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  details: { padding: space.lg, gap: space.sm },
  detail: { flexDirection: 'row', alignItems: 'center', gap: space.md },
})
