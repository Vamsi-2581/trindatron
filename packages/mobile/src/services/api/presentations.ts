import { apiRequest } from './client'

export interface PresentationCapability {
  available: boolean
  max_slides: number
}

/** Whether this user can have a deck built right now (plan, kill switch, engine health and remaining budget). */
export function getPresentationCapability(signal?: AbortSignal): Promise<PresentationCapability> {
  return apiRequest<PresentationCapability>('/presentations/capability', { signal })
}
