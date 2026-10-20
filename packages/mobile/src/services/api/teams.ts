import { apiRequest } from './client'

export type SeatType = 'standard' | 'premium'

export interface TeamPricing {
  seats: Record<SeatType, { price_usd: string; entitlement: string }>
  min_seats: number
  max_seats: number
  currency: string
  billing_period: string
}

export interface TeamMember {
  id: string
  email: string
  role: 'owner' | 'admin' | 'member'
  seat_type: SeatType
  status: 'invited' | 'pending' | 'active'
  joined_at: string | null
}

export interface Team {
  id: string
  name: string
  status: string
  my_role: 'owner' | 'admin' | 'member'
  my_seat_type: SeatType
  current_period_end: string | null
  seats: Record<SeatType, { purchased: number; used: number; price_usd: string }>
  pending_seats: Record<SeatType, number> | null
  monthly_total_usd: string
  /** Only for the owner and admins. */
  members?: TeamMember[]
}

export interface TeamCheckout {
  order: { id: string; status: string; amount_fiat: string; currency_fiat: string; expires_at: string }
  seats: Record<SeatType, number>
  redirect_url: string | null
}

export interface TeamInvite {
  member_id: string
  email: string
  seat_type: SeatType
  invite_token: string
  expires_at: string
}

export function getTeamPricing(signal?: AbortSignal): Promise<TeamPricing> {
  return apiRequest<TeamPricing>('/teams/pricing', { signal })
}

/** The caller's team; a 404 means the account is not in one. */
export function getMyTeam(signal?: AbortSignal): Promise<Team> {
  return apiRequest<Team>('/teams/me', { signal })
}

export function createTeam(name: string, ownerSeatType: SeatType): Promise<Team> {
  return apiRequest<Team>('/teams', { method: 'POST', body: { name, owner_seat_type: ownerSeatType } })
}

export function startTeamCheckout(teamId: string, seats: Record<SeatType, number>, idempotencyKey: string): Promise<TeamCheckout> {
  return apiRequest<TeamCheckout>(`/teams/${encodeURIComponent(teamId)}/checkout`, {
    method: 'POST',
    body: { standard_seats: seats.standard, premium_seats: seats.premium, idempotency_key: idempotencyKey },
  })
}

export function inviteMember(teamId: string, email: string, seatType: SeatType): Promise<TeamInvite> {
  return apiRequest<TeamInvite>(`/teams/${encodeURIComponent(teamId)}/invites`, { method: 'POST', body: { email, seat_type: seatType } })
}

export function acceptInvite(token: string): Promise<Team> {
  return apiRequest<Team>('/teams/invites/accept', { method: 'POST', body: { token } })
}

export function removeMember(teamId: string, memberId: string): Promise<void> {
  return apiRequest<void>(`/teams/${encodeURIComponent(teamId)}/members/${encodeURIComponent(memberId)}`, { method: 'DELETE' })
}
