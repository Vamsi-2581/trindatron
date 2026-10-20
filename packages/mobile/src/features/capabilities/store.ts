import { create } from 'zustand'

import { apiRequest } from '@/services/api/client'
import { getPresentationCapability } from '@/services/api/presentations'
import { getEntitlement } from '@/services/api/usage'
import { markBoot, timeBootStep } from '@/services/bootTimeline'

import {
  baselineCapabilities,
  capabilitiesFromEntitlement,
  CORE_CAPABILITIES_PATH,
  parseCoreCapabilities,
  statusOf,
  type CapabilityId,
  type CapabilityState,
  type CapabilityStatus,
  type Capabilities,
} from './capabilities'

interface CapabilityStore {
  capabilities: Capabilities
  /** The Core answered at least once for the current user. */
  loaded: boolean
  userId: string | null
  load(userId: string | null): Promise<void>
}

async function fetchCapabilities(): Promise<Capabilities> {
  if (CORE_CAPABILITIES_PATH) {
    try {
      const parsed = parseCoreCapabilities(await apiRequest<unknown>(CORE_CAPABILITIES_PATH))
      if (parsed) return parsed
    } catch {
      // An older Core without the route: derive what is safe from the entitlement below.
    }
  }
  const [entitlement, presentations] = await Promise.all([
    getEntitlement(),
    getPresentationCapability().catch(() => null),
  ])
  return capabilitiesFromEntitlement(entitlement, { presentations: presentations?.available === true })
}

/** One source for "what can this user do right now", always answered by the Core. */
export const useCapabilityStore = create<CapabilityStore>()((set, get) => ({
  capabilities: baselineCapabilities(),
  loaded: false,
  userId: null,

  async load(userId) {
    if (get().userId !== userId) set({ capabilities: baselineCapabilities(), loaded: false, userId })
    try {
      const capabilities = await timeBootStep('capabilitiesRequest', fetchCapabilities())
      if (get().userId === userId) {
        set({ capabilities, loaded: true })
        markBoot('capabilities')
      }
    } catch {
      // Keep what is known; a failed read never turns a capability on.
    }
  },
}))

/** One capability, for a screen that needs exactly one. */
export function useCapability(id: CapabilityId): CapabilityState {
  return useCapabilityStore((state) => state.capabilities[id])
}

/** Why this capability is off, so a screen can say the true reason instead of "unavailable". */
export function useCapabilityStatus(id: CapabilityId): CapabilityStatus {
  return useCapabilityStore((state) => statusOf(state.capabilities[id]))
}
