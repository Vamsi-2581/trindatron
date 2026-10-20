import { apiRequest } from './client'
import type { Capacity, Entitlement } from './types'

/** Percentages and renewal instants only — the Core keeps TCU out of this view. */
export function getCapacity(signal?: AbortSignal): Promise<Capacity> {
  return apiRequest<Capacity>('/users/me/capacity', { signal })
}

export function getEntitlement(signal?: AbortSignal): Promise<Entitlement> {
  return apiRequest<Entitlement>('/users/me/entitlement', { signal })
}
