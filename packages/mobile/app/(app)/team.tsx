import * as WebBrowser from 'expo-web-browser'
import { useRef, useState } from 'react'
import { Platform, Share, StyleSheet, View } from 'react-native'

import { AppText, Badge, Button, Chip, ErrorState, ListRow, SkeletonRows, Notice, Screen, Section, Surface, TextField } from '@/components/ui'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useT } from '@/i18n'
import { toApiError, userMessage } from '@/services/api/errors'
import {
  acceptInvite,
  createTeam,
  getMyTeam,
  getTeamPricing,
  inviteMember,
  removeMember,
  startTeamCheckout,
  type SeatType,
  type Team,
  type TeamInvite,
  type TeamPricing,
} from '@/services/api/teams'
import { space } from '@/theme'
import { confirmAction } from '@/utils/confirm'
import { formatPrice } from '@/utils/format'
import { createId } from '@/utils/ids'

const SEATS: SeatType[] = ['standard', 'premium']

/** The caller's team, or null when the account is not in one (the Core answers 404). */
async function loadTeam(signal: AbortSignal): Promise<{ team: Team | null; pricing: TeamPricing }> {
  const pricing = await getTeamPricing(signal)
  try {
    return { team: await getMyTeam(signal), pricing }
  } catch (cause) {
    if (toApiError(cause).kind === 'not_found') return { team: null, pricing }
    throw cause
  }
}

/** Team prices are quoted in dollars by the Core; written the way the person's language writes money, like every plan price. */
function usd(value: string | number): string {
  return formatPrice(String(value), 'USD')
}

/**
 * TEAM by seat. Prices, seat limits, totals and activation all come from the
 * Core; the app only collects the choice and shows the result.
 */
export default function TeamScreen() {
  const t = useT()
  const resource = useAsyncResource(loadTeam, [], { refetchOnFocus: true })

  if (resource.loading && !resource.data) {
    return (
      <Screen>
        <SkeletonRows count={4} />
      </Screen>
    )
  }
  if (resource.error || !resource.data) {
    return (
      <Screen>
        <ErrorState message={resource.error?.message ?? t('error.generic')} onRetry={resource.refresh} />
      </Screen>
    )
  }
  const { team, pricing } = resource.data
  return team ? (
    <TeamDetail team={team} onChanged={resource.refresh} />
  ) : (
    <NoTeam pricing={pricing} onChanged={(next) => resource.setData({ team: next, pricing })} />
  )
}

function SeatCounter({ label, detail, value, onChange, min }: { label: string; detail: string; value: number; onChange: (next: number) => void; min: number }) {
  const t = useT()
  return (
    <View style={styles.counter}>
      <View style={styles.flex}>
        <AppText variant="bodyStrong">{label}</AppText>
        <AppText variant="caption" tone="secondary">
          {detail}
        </AppText>
      </View>
      <Button label="−" variant="secondary" accessibilityLabel={t('team.fewer', { seat: label })} disabled={value <= min} onPress={() => onChange(value - 1)} />
      <AppText variant="title" style={styles.count} accessibilityLabel={t('team.seatCount', { count: value, seat: label })}>
        {value}
      </AppText>
      <Button label="+" variant="secondary" accessibilityLabel={t('team.more', { seat: label })} onPress={() => onChange(value + 1)} />
    </View>
  )
}

function NoTeam({ pricing, onChanged }: { pricing: TeamPricing; onChanged: (team: Team) => void }) {
  const t = useT()
  const [name, setName] = useState('')
  const [seats, setSeats] = useState<Record<SeatType, number>>({ standard: pricing.min_seats, premium: 0 })
  const [ownerSeat, setOwnerSeat] = useState<SeatType>('standard')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [token, setToken] = useState('')
  const checkoutKey = useRef(createId())
  const total = seats.standard + seats.premium
  const monthly = SEATS.reduce((sum, seat) => sum + Number(pricing.seats[seat].price_usd) * seats[seat], 0)
  const valid = name.trim().length > 0 && total >= pricing.min_seats && total <= pricing.max_seats && seats[ownerSeat] > 0

  async function create() {
    if (!valid || busy) return
    setBusy(true)
    setError(null)
    try {
      const team = await createTeam(name.trim(), ownerSeat)
      const checkout = await startTeamCheckout(team.id, seats, checkoutKey.current)
      if (checkout.redirect_url) await WebBrowser.openBrowserAsync(checkout.redirect_url)
      onChanged(await getMyTeam())
    } catch (cause) {
      setError(userMessage(cause))
    } finally {
      setBusy(false)
    }
  }

  async function accept() {
    if (!token.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      onChanged(await acceptInvite(token.trim()))
    } catch (cause) {
      setError(userMessage(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Screen>
      <View style={styles.header}>
        <AppText variant="title">{t('team.noTeamTitle')}</AppText>
        <AppText tone="secondary">{t('team.noTeamBody', { min: pricing.min_seats, max: pricing.max_seats })}</AppText>
      </View>

      {error ? <Notice tone="error">{error}</Notice> : null}

      {Platform.OS === 'ios' ? (
        <Notice>{t('billing.iosUnavailable')}</Notice>
      ) : (
        <Surface style={styles.card}>
          <TextField label={t('team.name')} value={name} onChangeText={setName} maxLength={120} />
          {SEATS.map((seat) => (
            <SeatCounter
              key={seat}
              label={t(`team.seat.${seat}`)}
              detail={t(`team.seatDetail.${seat}`, { price: usd(pricing.seats[seat].price_usd) })}
              value={seats[seat]}
              min={0}
              onChange={(next) => setSeats((current) => ({ ...current, [seat]: Math.max(0, Math.min(pricing.max_seats, next)) }))}
            />
          ))}
          <View style={styles.chips}>
            <AppText variant="caption" tone="secondary">
              {t('team.mySeat')}
            </AppText>
            {SEATS.map((seat) => {
              const selected = ownerSeat === seat
              return (
                <Chip key={seat} label={t(`team.seat.${seat}`)} selected={selected} onPress={() => setOwnerSeat(seat)} />
              )
            })}
          </View>
          <AppText variant="bodyStrong">{t('team.total', { count: total, price: usd(monthly) })}</AppText>
          {total > 0 && (total < pricing.min_seats || total > pricing.max_seats) ? (
            <AppText variant="caption" tone="error">
              {t('error.code.invalid_seats')}
            </AppText>
          ) : null}
          <Button label={t('team.create')} icon="card-outline" loading={busy} disabled={!valid} onPress={create} fullWidth />
          <AppText variant="caption" tone="muted">
            {t('team.activationNote')}
          </AppText>
        </Surface>
      )}

      <Section title={t('team.acceptTitle')}>
        <Surface style={styles.card}>
          <TextField label={t('team.acceptLabel')} value={token} onChangeText={setToken} autoCapitalize="none" autoCorrect={false} />
          <Button label={t('team.accept')} variant="secondary" loading={busy} disabled={!token.trim()} onPress={accept} />
        </Surface>
      </Section>
    </Screen>
  )
}

function TeamDetail({ team, onChanged }: { team: Team; onChanged: () => void }) {
  const t = useT()
  const [email, setEmail] = useState('')
  const [seat, setSeat] = useState<SeatType>('standard')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [invite, setInvite] = useState<TeamInvite | null>(null)
  const manages = team.my_role === 'owner' || team.my_role === 'admin'
  const statusKey = team.status === 'active' || team.status === 'pending_payment' ? team.status : 'inactive'

  async function sendInvite() {
    if (!email.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      const created = await inviteMember(team.id, email.trim(), seat)
      setInvite(created)
      setEmail('')
      onChanged()
    } catch (cause) {
      setError(userMessage(cause))
    } finally {
      setBusy(false)
    }
  }

  async function remove(memberId: string, memberEmail: string) {
    const confirmed = await confirmAction({
      title: t('team.removeTitle', { email: memberEmail }),
      message: t('team.removeMessage'),
      confirmLabel: t('team.remove'),
      destructive: true,
    })
    if (!confirmed) return
    try {
      await removeMember(team.id, memberId)
      onChanged()
    } catch (cause) {
      setError(userMessage(cause))
    }
  }

  return (
    <Screen>
      <View style={styles.header}>
        <AppText variant="title">{team.name}</AppText>
        <View style={styles.chips}>
          <Badge label={t(`team.status.${statusKey}`)} tone={team.status === 'active' ? 'success' : 'warning'} />
          <Badge label={t('team.mySeatValue', { seat: t(`team.seat.${team.my_seat_type}`) })} tone="accent" />
        </View>
        {team.status === 'pending_payment' ? <Notice>{t('team.pendingPayment')}</Notice> : null}
        {team.current_period_end ? (
          <AppText variant="caption" tone="secondary">
            {t('team.periodEnd', { date: new Date(team.current_period_end).toLocaleDateString() })}
          </AppText>
        ) : null}
      </View>

      {error ? <Notice tone="error">{error}</Notice> : null}

      <Section title={t('team.seats')}>
        <Surface>
          {SEATS.map((type) => (
            <ListRow
              key={type}
              icon={type === 'premium' ? 'diamond-outline' : 'person-outline'}
              title={t(`team.seat.${type}`)}
              subtitle={t('team.seatsUsed', { used: team.seats[type].used, purchased: team.seats[type].purchased })}
              trailing={<AppText variant="caption">{usd(team.seats[type].price_usd)}</AppText>}
            />
          ))}
          <ListRow icon="card-outline" title={t('team.monthly')} trailing={<AppText variant="bodyStrong">{usd(team.monthly_total_usd)}</AppText>} />
        </Surface>
        {team.pending_seats ? (
          <AppText variant="caption" tone="secondary">
            {t('team.pendingChange', { standard: team.pending_seats.standard, premium: team.pending_seats.premium })}
          </AppText>
        ) : null}
      </Section>

      {manages && team.members ? (
        <Section title={t('team.members')}>
          <Surface>
            {team.members.map((member) => (
              <ListRow
                key={member.id}
                icon="person-circle-outline"
                title={member.email}
                subtitle={`${t(`team.role.${member.role}`)} · ${t(`team.seat.${member.seat_type}`)} · ${t(`team.memberStatus.${member.status}`)}`}
                trailing={
                  member.role !== 'owner' ? (
                    <Button label={t('team.remove')} variant="ghost" onPress={() => void remove(member.id, member.email)} />
                  ) : undefined
                }
              />
            ))}
          </Surface>
        </Section>
      ) : null}

      {manages && team.status === 'active' ? (
        <Section title={t('team.invite')}>
          <Surface style={styles.card}>
            <TextField label={t('team.inviteEmail')} value={email} onChangeText={setEmail} autoCapitalize="none" keyboardType="email-address" />
            <View style={styles.chips}>
              {SEATS.map((type) => (
                <Chip key={type} label={t(`team.seat.${type}`)} selected={seat === type} onPress={() => setSeat(type)} />
              ))}
            </View>
            <Button label={t('team.invite')} icon="mail-outline" loading={busy} disabled={!email.trim()} onPress={sendInvite} />
            {invite ? (
              <View style={styles.invite}>
                <AppText variant="caption">{t('team.inviteCreated', { email: invite.email, date: new Date(invite.expires_at).toLocaleDateString() })}</AppText>
                <AppText variant="caption" selectable>
                  {invite.invite_token}
                </AppText>
                <Button
                  label={t('team.shareInvite')}
                  variant="secondary"
                  icon="share-outline"
                  onPress={() => void Share.share({ message: t('team.inviteMessage', { token: invite.invite_token }) })}
                />
              </View>
            ) : null}
          </Surface>
        </Section>
      ) : null}
    </Screen>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  header: { gap: space.sm },
  card: { padding: space.lg, gap: space.md },
  counter: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  count: { minWidth: 36, textAlign: 'center' },
  chips: { flexDirection: 'row', flexWrap: 'wrap', alignItems: 'center', gap: space.sm },
  invite: { gap: space.sm },
})
