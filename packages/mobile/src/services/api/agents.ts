import { apiRequest } from './client'
import type { AgentDefinition, AgentInstance, ExecutionJob, ExecutionJobStatus } from './types'

/** The agent runtime lives in the Core. These calls only read and steer it. */

export function listAgentDefinitions(signal?: AbortSignal): Promise<{ agents: AgentDefinition[]; total: number }> {
  return apiRequest('/agents', { signal })
}

export function listAgentInstances(signal?: AbortSignal): Promise<{ instances: AgentInstance[]; total: number }> {
  return apiRequest('/agent-instances', { signal })
}

export function pauseAgentInstance(instanceId: string): Promise<AgentInstance> {
  return apiRequest(`/agent-instances/${encodeURIComponent(instanceId)}/pause`, { method: 'POST' })
}

export function resumeAgentInstance(instanceId: string): Promise<AgentInstance> {
  return apiRequest(`/agent-instances/${encodeURIComponent(instanceId)}/resume`, { method: 'POST' })
}

export function runAgentInstance(instanceId: string): Promise<ExecutionJob> {
  return apiRequest(`/agent-instances/${encodeURIComponent(instanceId)}/run`, { method: 'POST' })
}

export function listExecutionJobs(status?: ExecutionJobStatus, signal?: AbortSignal): Promise<{ jobs: ExecutionJob[]; total: number }> {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  return apiRequest(`/execution/jobs${query}`, { signal })
}

export function getExecutionJob(jobId: string, signal?: AbortSignal): Promise<ExecutionJob> {
  return apiRequest(`/execution/jobs/${encodeURIComponent(jobId)}`, { signal })
}

export function cancelExecutionJob(jobId: string): Promise<ExecutionJob> {
  return apiRequest(`/execution/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' })
}

const TERMINAL: ReadonlySet<ExecutionJobStatus> = new Set(['completed', 'failed', 'cancelled', 'expired', 'destroyed'])

export function isTerminalJobStatus(status: ExecutionJobStatus): boolean {
  return TERMINAL.has(status)
}
