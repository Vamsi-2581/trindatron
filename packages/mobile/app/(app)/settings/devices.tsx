import Ionicons from '@expo/vector-icons/Ionicons'
import { router } from 'expo-router'
import { useState } from 'react'
import { StyleSheet, View } from 'react-native'

import { ActionMenu, AppText, Badge, Button, EmptyState, ErrorState, Notice, Screen, SkeletonRows, Surface } from '@/components/ui'
import { describeDevice } from '@/features/settings/deviceName'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useT } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import { listDeviceSessions, revokeDeviceSession, signOutEverywhere, type DeviceSession } from '@/services/api/settings'
import { useAuthStore } from '@/stores/auth'
import { iconSize, radius, space, useTheme } from '@/theme'
import { confirmAction } from '@/utils/confirm'
import { formatDateTime } from '@/utils/dates'

/**
 * Where this account is signed in, and how to end any of it.
 *
 * Only what the Core reports is shown — what kind of client and browser the session's user agent says it was,
 * when it started and when it was last used — named the way a person recognises it ("Chrome no Windows") instead
 * of the raw string. No token, no location, nothing inferred beyond that string.
 */
export default function DevicesScreen() {
  const { colors } = useTheme()
  const t = useT()
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const sessions = useAsyncResource((signal) => listDeviceSessions(signal), [], { refetchOnFocus: true })

  function deviceName(session: DeviceSession): string {
    const device = describeDevice(session.user_agent)
    const os = t(`devices.platform.${device.platform}`)
    if (device.kind === 'app') return device.platform === 'unknown' ? t('devices.name.app') : t('devices.name.appOn', { os })
    if (device.kind === 'browser' && device.browser) {
      return device.platform === 'unknown' ? device.browser : t('devices.name.browserOn', { browser: device.browser, os })
    }
    return t('devices.name.unknown')
  }

  async function revoke(session: DeviceSession) {
    const confirmed = await confirmAction({
      title: t('devices.revokeTitle'),
      message: t('devices.revokeMessage', { device: deviceName(session) }),
      confirmLabel: t('devices.revoke'),
      destructive: true,
    })
    if (!confirmed) return
    setBusy(session.id)
    setError(null)
    try {
      await revokeDeviceSession(session.id)
      await sessions.refresh()
    } catch (cause) {
      setError(userMessage(cause))
    } finally {
      setBusy(null)
    }
  }

  async function revokeAll() {
    const confirmed = await confirmAction({
      title: t('devices.signOutAllTitle'),
      message: t('devices.signOutAllMessage'),
      confirmLabel: t('devices.signOutAll'),
      destructive: true,
    })
    if (!confirmed) return
    setBusy('all')
    setError(null)
    try {
      await signOutEverywhere()
      // This device's session ended too: the app returns to sign-in.
      await useAuthStore.getState().signOut()
      router.replace('/login')
    } catch (cause) {
      setError(userMessage(cause))
      setBusy(null)
    }
  }

  // The device in hand first, then the most recently used.
  const list = [...(sessions.data?.sessions ?? [])].sort(
    (a, b) => Number(b.current) - Number(a.current) || b.last_used_at.localeCompare(a.last_used_at),
  )

  return (
    <Screen edges={['bottom', 'left', 'right']} refreshing={sessions.refreshing} onRefresh={sessions.refresh}>
      {error ? <Notice tone="error">{error}</Notice> : null}

      {sessions.loading && !sessions.data ? (
        <SkeletonRows count={3} icon />
      ) : sessions.error && !sessions.data ? (
        <ErrorState message={sessions.error.message} onRetry={sessions.refresh} />
      ) : list.length === 0 ? (
        <EmptyState icon="phone-portrait-outline" title={t('devices.emptyTitle')} body={t('devices.emptyBody')} />
      ) : (
        <Surface>
          {list.map((session, index) => {
            const device = describeDevice(session.user_agent)
            const name = deviceName(session)
            return (
              <View key={session.id} style={[styles.row, index > 0 && { borderTopColor: colors.separator, borderTopWidth: StyleSheet.hairlineWidth }]}>
                <View style={[styles.icon, { backgroundColor: colors.surface2 }]}>
                  <Ionicons name={device.icon} size={iconSize.md} color={colors.textSecondary} />
                </View>
                <View style={styles.text}>
                  <View style={styles.title}>
                    <AppText variant="bodyStrong" numberOfLines={2} style={styles.shrink}>
                      {name}
                    </AppText>
                    {session.current ? <Badge label={t('devices.thisDevice')} tone="accent" /> : null}
                  </View>
                  <AppText variant="caption" tone="muted" numberOfLines={1}>
                    {t('devices.lastUsed', { when: formatDateTime(session.last_used_at) })}
                  </AppText>
                  <AppText variant="caption" tone="muted" numberOfLines={1}>
                    {t('devices.startedAt', { when: formatDateTime(session.started_at) })}
                  </AppText>
                </View>
                {session.current || busy === session.id ? null : (
                  <ActionMenu
                    label={t('devices.actions', { device: name })}
                    items={[{ id: 'revoke', title: t('devices.revoke'), icon: 'log-out-outline', destructive: true }]}
                    onSelect={(id) => id === 'revoke' && void revoke(session)}
                  />
                )}
              </View>
            )
          })}
        </Surface>
      )}

      <View style={styles.footer}>
        <Button
          label={t('devices.signOutAll')}
          variant="danger"
          icon="log-out-outline"
          fullWidth
          loading={busy === 'all'}
          disabled={busy !== null}
          onPress={() => void revokeAll()}
        />
        <AppText variant="caption" tone="muted" style={styles.center}>
          {t('devices.signOutAllNote')}
        </AppText>
      </View>
    </Screen>
  )
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', alignItems: 'center', gap: space.md, paddingLeft: space.lg, paddingRight: space.xs, paddingVertical: space.md },
  icon: { width: 40, height: 40, borderRadius: radius.full, alignItems: 'center', justifyContent: 'center' },
  text: { flex: 1, gap: 2 },
  // Wraps: the badge goes under a long name rather than cutting it to "App TRINDATRON no An…" on a phone.
  title: { flexDirection: 'row', flexWrap: 'wrap', alignItems: 'center', columnGap: space.sm, rowGap: 2 },
  shrink: { flexShrink: 1 },
  footer: { gap: space.sm, marginTop: space.lg },
  center: { textAlign: 'center' },
})
