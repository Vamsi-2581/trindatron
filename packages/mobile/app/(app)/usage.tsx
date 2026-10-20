import Ionicons from '@expo/vector-icons/Ionicons'
import { router } from 'expo-router'
import { useEffect } from 'react'
import { StyleSheet, View } from 'react-native'

import { AppText, Button, ErrorState, SkeletonRows, Notice, ProgressBar, Screen, Section, Surface } from '@/components/ui'
import type { MediaCapability } from '@/features/capabilities/capabilities'
import { useCapabilityStore } from '@/features/capabilities/store'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useT } from '@/i18n'
import type { EntitlementFeatures } from '@/services/api/types'
import { PLANS_HREF } from '@/features/navigation/routes'
import { getCapacity, getEntitlement } from '@/services/api/usage'
import { useAuthStore } from '@/stores/auth'
import { iconSize, space, useTheme } from '@/theme'
import { formatClock, formatWeekday, isSameLocalDay } from '@/utils/dates'
import { formatPercent, planLabel } from '@/utils/format'

const FEATURE_KEYS: Record<keyof EntitlementFeatures, string> = {
  supports_agent_mode: 'usage.feature.agentMode',
  supports_trinity_code: 'usage.feature.code',
  supports_trinity_work: 'usage.feature.work',
  supports_image_generation: 'usage.feature.image',
}

/** Daily creation allowances, shown only when the Core reports one — the app never counts them itself. */
const MEDIA_QUOTAS: { capability: MediaCapability; labelKey: string }[] = [
  { capability: 'image_generation', labelKey: 'usage.media.imagesToday' },
  { capability: 'video_generation', labelKey: 'usage.media.videosToday' },
]

/**
 * Plan usage as a product: the current session window, this week, when each
 * resets and how much is left — all from GET /users/me/capacity, formatted in
 * the user's language and time zone. TCU is an internal unit and never shown.
 */
export default function UsageScreen() {
  const t = useT()
  const user = useAuthStore((state) => state.user)
  const capacity = useAsyncResource((signal) => getCapacity(signal), [], { refetchOnFocus: true })
  const entitlement = useAsyncResource((signal) => getEntitlement(signal), [])
  const capabilities = useCapabilityStore((state) => state.capabilities)
  const userId = user?.id ?? null
  useEffect(() => {
    void useCapabilityStore.getState().load(userId)
  }, [userId])
  const mediaQuotas = MEDIA_QUOTAS.flatMap(({ capability, labelKey }) => {
    const state = capabilities[capability]
    return state.available && state.dailyQuota ? [{ capability, labelKey, quota: state.dailyQuota }] : []
  })

  const cap = capacity.data
  const sessionReset = cap?.session.resets_at
    ? isSameLocalDay(cap.session.resets_at)
      ? t('usage.resetsAt', { time: formatClock(cap.session.resets_at) })
      : t('usage.resetsOn', { day: formatWeekday(cap.session.resets_at), time: formatClock(cap.session.resets_at) })
    : null
  const agentsAllowed = entitlement.data ? entitlement.data.limits.max_agents !== 0 : false

  const featureKeys = Object.keys(FEATURE_KEYS) as (keyof EntitlementFeatures)[]
  const features = {
    included: featureKeys.filter((key) => entitlement.data?.features[key]),
    missing: entitlement.data ? featureKeys.filter((key) => !entitlement.data?.features[key]) : [],
  }

  return (
    <Screen
      edges={['bottom', 'left', 'right']}
      refreshing={capacity.refreshing || entitlement.refreshing}
      onRefresh={() => {
        void capacity.refresh()
        void entitlement.refresh()
      }}
    >
      <View style={styles.planRow}>
        <View style={styles.flex}>
          <AppText variant="label" tone="muted">
            {t('usage.currentPlan')}
          </AppText>
          <AppText variant="title">{planLabel(user?.plan)}</AppText>
        </View>
        <Button label={t('usage.seePlans')} variant="secondary" onPress={() => router.push(PLANS_HREF)} />
      </View>

      {capacity.loading && !cap ? (
        <SkeletonRows count={4} />
      ) : !cap ? (
        <ErrorState message={capacity.error?.message ?? t('usage.loadFailed')} onRetry={capacity.refresh} />
      ) : (
        <>
          <Surface style={styles.card}>
            <AppText variant="label" tone="muted" accessibilityRole="header">
              {t('usage.session')}
            </AppText>
            {cap.session.active && cap.session.pct_used !== null ? (
              <>
                <View style={styles.between}>
                  <AppText variant="title">{t('usage.used', { percent: formatPercent(cap.session.pct_used) })}</AppText>
                  <AppText variant="caption" tone="secondary">
                    {t('usage.remaining', { percent: formatPercent(100 - cap.session.pct_used) })}
                  </AppText>
                </View>
                <ProgressBar value={cap.session.pct_used} label={t('usage.sessionBar')} />
                {sessionReset ? <AppText tone="secondary">{sessionReset}</AppText> : null}
              </>
            ) : (
              <AppText tone="secondary">{t('usage.noSession')}</AppText>
            )}
          </Surface>

          <Surface style={styles.card}>
            <AppText variant="label" tone="muted" accessibilityRole="header">
              {t('usage.week')}
            </AppText>
            <View style={styles.between}>
              <AppText variant="title">{t('usage.used', { percent: formatPercent(cap.weekly.pct_used ?? 0) })}</AppText>
              <AppText variant="caption" tone="secondary">
                {t('usage.remaining', { percent: formatPercent(100 - (cap.weekly.pct_used ?? 0)) })}
              </AppText>
            </View>
            <ProgressBar value={cap.weekly.pct_used ?? 0} label={t('usage.weekBar')} />
            {cap.weekly.resets_at ? (
              <AppText tone="secondary">
                {t('usage.weekResets', { day: formatWeekday(cap.weekly.resets_at), time: formatClock(cap.weekly.resets_at) })}
              </AppText>
            ) : null}
          </Surface>

          <AppText variant="caption" tone="secondary">
            {t('usage.capacityNow', { percent: formatPercent(cap.capacity_remaining_pct) })}
            {agentsAllowed ? ` · ${cap.active_agents === 1 ? t('usage.agentsOne') : t('usage.agentsMany', { count: cap.active_agents })}` : ''}
          </AppText>
          {cap.optimizing ? <Notice tone="info">{t('usage.optimizing')}</Notice> : null}
        </>
      )}

      {mediaQuotas.length > 0 ? (
        <Surface style={styles.card}>
          <AppText variant="label" tone="muted" accessibilityRole="header">
            {t('usage.media.title')}
          </AppText>
          {mediaQuotas.map(({ capability, labelKey, quota }) => (
            <View key={capability} style={styles.quota}>
              <View style={styles.between}>
                <AppText>{t(labelKey)}</AppText>
                <AppText variant="bodyStrong">{t('usage.media.remainingOf', { remaining: quota.remaining, limit: quota.limit })}</AppText>
              </View>
              <ProgressBar value={quota.limit > 0 ? ((quota.limit - quota.remaining) / quota.limit) * 100 : 100} label={t(labelKey)} />
            </View>
          ))}
          <Button label={t('creations.title')} variant="ghost" icon="images-outline" onPress={() => router.push('/creations')} />
        </Surface>
      ) : null}

      {entitlement.data ? (
        <Section title={t('usage.included')}>
          <Surface style={styles.card}>
            {/* Only what this plan has: the section used to list every feature under "Incluído", the missing ones too. */}
            {features.included.map((key) => (
              <FeatureRow key={key} feature={t(FEATURE_KEYS[key])} included />
            ))}
            <View style={styles.between}>
              <AppText variant="caption" tone="secondary">
                {t('usage.projects')}
              </AppText>
              <AppText variant="caption">{entitlement.data.limits.max_projects ?? t('common.unlimited')}</AppText>
            </View>
            <View style={styles.between}>
              <AppText variant="caption" tone="secondary">
                {t('usage.concurrentAgents')}
              </AppText>
              <AppText variant="caption">{entitlement.data.limits.max_agents ?? t('common.unlimited')}</AppText>
            </View>
          </Surface>
        </Section>
      ) : null}

      {features.missing.length > 0 ? (
        <Section title={t('usage.notIncluded')}>
          <Surface style={styles.card}>
            {features.missing.map((key) => (
              <FeatureRow key={key} feature={t(FEATURE_KEYS[key])} included={false} />
            ))}
          </Surface>
        </Section>
      ) : null}
    </Screen>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  planRow: { flexDirection: 'row', alignItems: 'center', gap: space.md },
  card: { padding: space.lg, gap: space.sm },
  between: { flexDirection: 'row', alignItems: 'baseline', justifyContent: 'space-between', gap: space.sm, flexWrap: 'wrap' },
  feature: { flexDirection: 'row', alignItems: 'center', gap: space.sm, minHeight: 32 },
  quota: { gap: space.xs },
})

function FeatureRow({ feature, included }: { feature: string; included: boolean }) {
  const { colors } = useTheme()
  const t = useT()
  return (
    <View
      style={styles.feature}
      accessible
      accessibilityLabel={included ? t('usage.featureIncluded', { feature }) : t('usage.featureNotIncluded', { feature })}
    >
      <Ionicons
        name={included ? 'checkmark-circle' : 'remove-circle-outline'}
        size={iconSize.md}
        color={included ? colors.success : colors.textMuted}
      />
      <AppText tone={included ? 'primary' : 'muted'}>{feature}</AppText>
    </View>
  )
}
