import type { User, Session, Message, DoneEvent, AgentStep, AgentDoneEvent, UserUsage, SessionStatus, Entitlement, Project, ProjectFile, CodeWorkspace, WorkWorkspace, Skill, AgentDefinition, WorkspaceFileEntry, Memory, CreditSummary, CreditBalance, PaymentOrder, PaymentOrderListResponse, PlanPrice, PaymentProvidersResponse, CheckoutResponse, VoiceTranscription, Capacity, ExecutionJob, AgentInstance, ApprovalRequiredEvent } from '../types'

type RawSSEEvent = {
  type: string
  content?: string
  message?: string  // PT-BR fallback string (backward compat)
  key?: string      // semantic i18n key (new protocol, e.g. "activity.research.searching")
  answer?: string
  session_id?: string
  message_id?: string
  iterations?: number
  tool?: string
  args?: Record<string, unknown>
  success?: boolean
  output?: string
  usage?: Record<string, number>
  /** A creation this turn started (image or video job). */
  artifact?: MediaArtifactEvent
  media_job_id?: string
}

/** The job a creation request in the conversation started. Never names an engine or a cost. */
export interface MediaArtifactEvent {
  id: string
  kind: string
  status: string
}

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'

// ── Session ───────────────────────────────────────────────────────────────
//
// The access token is short-lived and lives only in memory, so a script
// injected into the page cannot read a long-lived credential from storage.
// What keeps the user signed in is the refresh token, which the server sets
// as an httpOnly cookie scoped to /auth: JavaScript never sees it. On start-up
// and whenever a request comes back 401, the client exchanges that cookie for
// a new access token. Only a "was signed in" hint, which is no secret, is
// kept in localStorage so public pages can show the right navigation.

/** Pre-refresh builds kept a long-lived access token here. Removed on load. */
const LEGACY_TOKEN_KEY = 'trinity_token'
const SIGNED_IN_KEY = 'trindatron_signed_in'
const CLIENT_HEADER = { 'X-TRINDATRON-Client': 'web' }
/** A 401 from these means bad credentials, not an expired access token. */
const SESSION_ENDPOINTS = /\/auth\/(login|register|refresh|logout)$/

let accessToken: string | null = null
let refreshInFlight: Promise<boolean> | null = null
const expiredListeners = new Set<() => void>()

function storage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage
  } catch {
    return null
  }
}

storage()?.removeItem(LEGACY_TOKEN_KEY)

function markSignedIn(signedIn: boolean): void {
  const s = storage()
  if (!s) return
  if (signedIn) s.setItem(SIGNED_IN_KEY, '1')
  else s.removeItem(SIGNED_IN_KEY)
}

export function setToken(token: string | null): void {
  accessToken = token
}

/** The current in-memory access token, if any. */
export function getStoredToken(): string | null {
  return accessToken
}

/** Whether this browser had a session the last time it looked. A hint for
 * navigation only; bootstrapSession() is what establishes a session. */
export function hasSessionHint(): boolean {
  return storage()?.getItem(SIGNED_IN_KEY) === '1'
}

/** Called once the session cannot be recovered (refresh refused). */
export function onSessionExpired(listener: () => void): () => void {
  expiredListeners.add(listener)
  return () => expiredListeners.delete(listener)
}

function endSessionLocally(): void {
  accessToken = null
  markSignedIn(false)
}

/**
 * Exchange the refresh cookie for a new access token. Single-flight: any
 * number of concurrent callers share one request, because every refresh
 * rotates the cookie. A refusal ends the session; a network failure does
 * not, so a dropped connection never signs anyone out.
 */
export function refreshSession(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight
  refreshInFlight = (async () => {
    try {
      const resp = await fetch(`${API_BASE}/auth/refresh`, {
        method: 'POST',
        credentials: 'include',
        headers: CLIENT_HEADER,
      })
      if (resp.ok) {
        const body = (await resp.json()) as { access_token: string }
        accessToken = body.access_token
        markSignedIn(true)
        return true
      }
      if (resp.status === 401 || resp.status === 403) {
        endSessionLocally()
        expiredListeners.forEach((listener) => listener())
      }
      return false
    } catch {
      return false
    } finally {
      refreshInFlight = null
    }
  })()
  return refreshInFlight
}

/** Restore the session on start-up. True when the user is signed in. */
export async function bootstrapSession(): Promise<boolean> {
  if (accessToken) return true
  return refreshSession()
}

/**
 * fetch with the access token attached. A 401 is recovered once: the token
 * is refreshed and the request replayed. Auth endpoints are never replayed.
 */
async function authorizedFetch(url: string, init: RequestInit, headers: Record<string, string>): Promise<Response> {
  const send = () => {
    const h = { ...headers }
    if (accessToken) h['Authorization'] = `Bearer ${accessToken}`
    return fetch(url, { ...init, headers: h })
  }
  const resp = await send()
  if (resp.status !== 401 || SESSION_ENDPOINTS.test(url)) return resp
  return (await refreshSession()) ? send() : resp
}

async function apiFetch(path: string, options: RequestInit = {}): Promise<Response> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  Object.assign(headers, options.headers ?? {})
  return authorizedFetch(`${API_BASE}${path}`, options, headers)
}

// ── Auth ──────────────────────────────────────────────────────────────────

export interface AuthResponse {
  access_token: string
  token_type: string
  user_id: string
  email: string
  expires_in?: number | null
}

async function startSession(resp: Response): Promise<AuthResponse> {
  const body = (await resp.json()) as AuthResponse
  accessToken = body.access_token
  markSignedIn(true)
  return body
}

export async function apiRegister(
  email: string,
  password: string,
  fullName = '',
): Promise<AuthResponse> {
  const resp = await apiFetch('/auth/register', {
    method: 'POST',
    credentials: 'include',
    body: JSON.stringify({ email, password, full_name: fullName, client: 'web' }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    const detail = (err as { detail?: unknown }).detail
    const msg = typeof detail === 'string'
      ? detail
      : Array.isArray(detail)
        ? (detail[0] as { msg?: string })?.msg ?? 'Falha no registro'
        : 'Falha no registro'
    throw new Error(msg)
  }
  return startSession(resp)
}

export async function apiLogin(email: string, password: string): Promise<AuthResponse> {
  const resp = await apiFetch('/auth/login', {
    method: 'POST',
    credentials: 'include',
    body: JSON.stringify({ email, password, client: 'web' }),
  })
  if (resp.status === 429) throw new Error('Muitas tentativas. Aguarde alguns minutos ou redefina sua senha.')
  if (!resp.ok) throw new Error('Email ou senha inválidos')
  return startSession(resp)
}

// ── Account security ─────────────────────────────────────────────────────────
// None of these reveal whether an address has an account: "forgot password"
// answers the same for every address.

/** Ask for a reset link. Resolves the same way whether or not the address has an account. */
export async function apiForgotPassword(email: string): Promise<void> {
  const resp = await apiFetch('/auth/password/forgot', { method: 'POST', body: JSON.stringify({ email }) })
  if (resp.status === 429) throw new Error('Muitos pedidos. Tente de novo mais tarde.')
  if (!resp.ok) throw new Error('Não foi possível enviar agora. Tente de novo em instantes.')
}

const INVALID_LINK = 'Este link é inválido ou expirou. Peça um novo.'

export async function apiResetPassword(token: string, password: string): Promise<void> {
  const resp = await apiFetch('/auth/password/reset', { method: 'POST', body: JSON.stringify({ token, password }) })
  if (resp.status === 400) throw new Error(INVALID_LINK)
  if (resp.status === 422) throw new Error('A senha precisa ter pelo menos 8 caracteres.')
  if (resp.status === 429) throw new Error('Muitas tentativas. Tente de novo mais tarde.')
  if (!resp.ok) throw new Error('Não foi possível redefinir agora. Tente de novo em instantes.')
}

export async function apiVerifyEmail(token: string): Promise<void> {
  const resp = await apiFetch('/auth/email/verify', { method: 'POST', body: JSON.stringify({ token }) })
  if (resp.status === 400) throw new Error(INVALID_LINK)
  if (!resp.ok) throw new Error('Não foi possível confirmar agora. Tente de novo em instantes.')
}

/** Send a new confirmation link to the signed-in account. True when it was already confirmed. */
export async function apiResendVerification(): Promise<boolean> {
  const resp = await apiFetch('/auth/email/resend', { method: 'POST' })
  if (resp.status === 429) throw new Error('Muitos pedidos. Tente de novo mais tarde.')
  if (!resp.ok) throw new Error('Não foi possível enviar agora.')
  return ((await resp.json()) as { email_verified: boolean }).email_verified
}

/** Change the password. Every session ends, this one included: sign in again afterwards. */
export async function apiChangePassword(currentPassword: string, newPassword: string): Promise<void> {
  const resp = await apiFetch('/auth/password/change', {
    method: 'POST',
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  })
  if (resp.status === 400) throw new Error('A senha atual está incorreta.')
  if (resp.status === 422) throw new Error('A nova senha precisa ter pelo menos 8 caracteres.')
  if (resp.status === 429) throw new Error('Muitas tentativas. Aguarde um pouco.')
  if (!resp.ok) throw new Error('Não foi possível alterar a senha agora.')
}

export interface SecurityEvent {
  kind: string
  label: string
  method: 'password' | 'google' | null
  ip: string | null
  user_agent: string | null
  created_at: string
}

export async function apiListSecurityEvents(): Promise<SecurityEvent[]> {
  return jsonOrThrow(await apiFetch('/auth/security-events'))
}

/** Where "Continuar com Google" sends the browser. The API runs the whole
 * OpenID Connect flow and returns to /auth/google with the session cookie set. */
export function googleSignInUrl(): string {
  return `${API_BASE}/auth/google/start`
}

/** Sign out: the server revokes the refresh token and clears the cookie.
 * Local state is cleared even when the network call fails. */
export async function apiLogout(): Promise<void> {
  try {
    await fetch(`${API_BASE}/auth/logout`, { method: 'POST', credentials: 'include', headers: CLIENT_HEADER })
  } catch {
    // Offline: the cookie outlives this tab, but the local session ends now.
  } finally {
    endSessionLocally()
  }
}

export async function apiGetMe(): Promise<User> {
  const resp = await apiFetch('/auth/me')
  if (!resp.ok) throw new Error('Não autenticado')
  return resp.json()
}

/** Persist the browser-detected IANA timezone. Drives the weekly reset
 * rule (Sunday 12:00 local) — the backend validates it's a real IANA zone. */
export async function apiUpdateTimezone(timezone: string): Promise<{ timezone: string | null }> {
  const resp = await apiFetch('/users/me/timezone', {
    method: 'PATCH',
    body: JSON.stringify({ timezone }),
  })
  if (!resp.ok) throw new Error('Falha ao salvar timezone')
  return resp.json()
}

// ── Sessions ──────────────────────────────────────────────────────────────

export interface SessionListResponse { sessions: Session[]; total: number }
export interface SessionDetailResponse extends Session { messages: Message[] }

export async function apiListSessions(): Promise<SessionListResponse> {
  const resp = await apiFetch('/sessions')
  if (!resp.ok) throw new Error('Falha ao carregar conversas')
  return resp.json()
}

export async function apiCreateSession(title?: string, projectId?: string, agentDefinitionId?: string): Promise<Session> {
  const resp = await apiFetch('/sessions', {
    method: 'POST',
    body: JSON.stringify({ title, project_id: projectId ?? null, agent_definition_id: agentDefinitionId ?? null }),
  })
  if (!resp.ok) throw new Error('Falha ao criar conversa')
  return resp.json()
}

export async function apiGetSession(id: string): Promise<SessionDetailResponse> {
  const resp = await apiFetch(`/sessions/${id}`)
  if (!resp.ok) throw new Error('Conversa não encontrada')
  return resp.json()
}

export async function apiDeleteSession(id: string): Promise<void> {
  await apiFetch(`/sessions/${id}`, { method: 'DELETE' })
}

export async function apiDeleteMessagesFrom(sessionId: string, messageId: string): Promise<void> {
  const resp = await apiFetch(`/sessions/${sessionId}/messages/from/${messageId}`, { method: 'DELETE' })
  if (!resp.ok) throw new Error('Falha ao limpar histórico')
}

// ── Entitlement ───────────────────────────────────────────────────────────

export async function apiGetEntitlement(): Promise<Entitlement> {
  const resp = await apiFetch('/users/me/entitlement')
  if (!resp.ok) throw new Error('Falha ao carregar entitlement')
  return resp.json()
}

// ── Usage ─────────────────────────────────────────────────────────────────

export async function apiGetUserUsage(): Promise<UserUsage> {
  const resp = await apiFetch('/users/me/usage')
  if (!resp.ok) throw new Error('Falha ao carregar uso')
  return resp.json()
}

/** Read-only status of the 5-hour rolling entitlement session. Never
 * starts or extends a session — that only happens server-side as a side
 * effect of an actual TCU-reserving operation. */
export async function apiGetSessionStatus(): Promise<SessionStatus> {
  const resp = await apiFetch('/users/me/session')
  if (!resp.ok) throw new Error('Falha ao carregar sessão')
  return resp.json()
}

/** User-facing capacity summary (Execution Runtime V1). Deliberately
 * carries no TCU/token/cost figure — see the Capacity type's own comment. */
export async function apiGetCapacity(): Promise<Capacity> {
  const resp = await apiFetch('/users/me/capacity')
  if (!resp.ok) throw new Error('Falha ao carregar capacidade')
  return resp.json()
}

// ── Execution jobs ────────────────────────────────────────────────────────

export interface CreateExecutionJobBody {
  job_type?: string
  payload: Record<string, unknown>
  priority?: 'critical' | 'high' | 'normal' | 'low'
  timeout_seconds?: number
  max_retries?: number
  agent_id?: string | null
  session_id?: string | null
  project_id?: string | null
  sentinel_criteria?: Record<string, unknown> | null
}

/** Returns as soon as the job is queued and dispatched server-side — the
 * work continues independently of this request (see execution_service.py).
 * Poll apiGetExecutionJob() for status. */
export async function apiCreateExecutionJob(body: CreateExecutionJobBody): Promise<ExecutionJob> {
  const resp = await apiFetch('/execution/jobs', { method: 'POST', body: JSON.stringify(body) })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error(extractErrorMessage(err, resp.status))
  }
  return resp.json()
}

export async function apiListExecutionJobs(status?: string): Promise<{ jobs: ExecutionJob[]; total: number }> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : ''
  const resp = await apiFetch(`/execution/jobs${qs}`)
  if (!resp.ok) throw new Error('Falha ao carregar execuções')
  return resp.json()
}

export async function apiGetExecutionJob(id: string): Promise<ExecutionJob> {
  const resp = await apiFetch(`/execution/jobs/${id}`)
  if (!resp.ok) throw new Error('Execução não encontrada')
  return resp.json()
}

export interface ExecutionArtifact {
  id: string
  filename: string
  content_type: string
  size_bytes: number
}

/** The files a job produced (metadata only; the bytes are reached through a link). */
export async function apiListExecutionArtifacts(jobId: string): Promise<ExecutionArtifact[]> {
  const resp = await apiFetch(`/execution/jobs/${jobId}/artifacts`)
  if (!resp.ok) throw new Error(extractErrorMessage(await resp.json().catch(() => ({})), resp.status))
  return ((await resp.json()) as { artifacts: ExecutionArtifact[] }).artifacts
}

/** A short-lived signed link to a job's file; `url` is null where storage cannot sign (local development). */
export async function apiGetExecutionArtifactLink(artifactId: string): Promise<{ url: string | null; filename: string }> {
  const resp = await apiFetch(`/execution/artifacts/${artifactId}/link`)
  if (!resp.ok) throw new Error(extractErrorMessage(await resp.json().catch(() => ({})), resp.status))
  return resp.json()
}

/** A job's file read with the session — the fallback where no signed link exists. */
export async function apiFetchExecutionArtifactBlob(artifactId: string): Promise<Blob> {
  const resp = await authorizedFetch(`${API_BASE}/execution/artifacts/${encodeURIComponent(artifactId)}/download`,
    { method: 'GET' }, {})
  if (!resp.ok) throw new Error(`Erro ${resp.status}`)
  return resp.blob()
}

export async function apiCancelExecutionJob(id: string): Promise<ExecutionJob> {
  const resp = await apiFetch(`/execution/jobs/${id}/cancel`, { method: 'POST' })
  if (!resp.ok) throw new Error('Falha ao cancelar execução')
  return resp.json()
}

// ── Agent instances (Persistent Agent Instance V1) ─────────────────────────
// Mounted at /agent-instances, deliberately distinct from /agents
// (AgentDefinition CRUD) — see src/api/routes/agent_instances.py's own
// docstring for why the two paths can't share a prefix.

export async function apiListAgentInstances(): Promise<{ instances: AgentInstance[]; total: number }> {
  const resp = await apiFetch('/agent-instances')
  if (!resp.ok) throw new Error('Falha ao carregar agentes persistentes')
  return resp.json()
}

export async function apiCreateAgentInstance(agentDefinitionId: string, name: string, projectId?: string): Promise<AgentInstance> {
  const resp = await apiFetch('/agent-instances', {
    method: 'POST',
    body: JSON.stringify({ agent_definition_id: agentDefinitionId, name, project_id: projectId ?? null }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error(extractErrorMessage(err, resp.status))
  }
  return resp.json()
}

export async function apiPauseAgentInstance(id: string): Promise<AgentInstance> {
  const resp = await apiFetch(`/agent-instances/${id}/pause`, { method: 'POST' })
  if (!resp.ok) throw new Error('Falha ao pausar agente')
  return resp.json()
}

export async function apiResumeAgentInstance(id: string): Promise<AgentInstance> {
  const resp = await apiFetch(`/agent-instances/${id}/resume`, { method: 'POST' })
  if (!resp.ok) throw new Error('Falha ao retomar agente')
  return resp.json()
}

export async function apiRunAgentInstance(id: string): Promise<ExecutionJob> {
  const resp = await apiFetch(`/agent-instances/${id}/run`, { method: 'POST' })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error(extractErrorMessage(err, resp.status))
  }
  return resp.json()
}

// ── 429 detail extraction helper ──────────────────────────────────────────

function extractErrorMessage(err: unknown, status: number): string {
  if (err && typeof err === 'object') {
    const detail = (err as Record<string, unknown>).detail
    if (typeof detail === 'string') return detail
    if (detail && typeof detail === 'object') {
      const msg = (detail as Record<string, unknown>).message
      if (typeof msg === 'string') return msg
    }
  }
  if (status === 429) return 'Limite de uso atingido. Tente novamente mais tarde.'
  return `Erro ${status}`
}

// ── Shared SSE parser ─────────────────────────────────────────────────────

async function parseSSEStream(
  resp: Response,
  onEvent: (event: RawSSEEvent) => boolean | void,
): Promise<void> {
  if (!resp.body) return
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const raw = line.slice(6).trim()
        if (raw === '[DONE]') return

        try {
          const event = JSON.parse(raw) as RawSSEEvent
          const stop = onEvent(event)
          if (stop === true) return
        } catch {
          // skip malformed
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

// ── Chat SSE ──────────────────────────────────────────────────────────────

export async function apiStreamChat(
  sessionId: string,
  content: string,
  onText: (chunk: string) => void,
  onDone: (event: DoneEvent) => void,
  onError: (message: string) => void,
  onStatus?: (message: string) => void,
): Promise<void> {
  let resp: Response
  try {
    resp = await apiFetch(`/sessions/${sessionId}/chat`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    })
  } catch {
    onError('Não foi possível conectar ao TRINDATRON (verifique se o servidor está rodando)')
    return
  }

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    onError(extractErrorMessage(err, resp.status))
    return
  }

  await parseSSEStream(resp, (event) => {
    if (event.type === 'status') {
      // Prefer semantic key (i18n-ready); fall back to PT-BR message string.
      onStatus?.(event.key ?? event.message ?? '')
    } else if (event.type === 'text' && event.content) {
      onText(event.content)
    } else if (event.type === 'done') {
      onDone(event as unknown as DoneEvent)
    } else if (event.type === 'error') {
      onError(event.message ?? 'Erro no stream')
      return true
    }
  })
}

// ── Projects ──────────────────────────────────────────────────────────────

export interface ProjectListResponse { projects: Project[]; total: number }

export async function apiListProjects(): Promise<ProjectListResponse> {
  const resp = await apiFetch('/projects')
  if (!resp.ok) throw new Error('Falha ao carregar projetos')
  return resp.json()
}

export async function apiCreateProject(
  name: string,
  description?: string,
  instructions?: string,
): Promise<Project> {
  const resp = await apiFetch('/projects', {
    method: 'POST',
    body: JSON.stringify({ name, description: description ?? null, instructions: instructions ?? null }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error((err as { detail?: string }).detail ?? 'Falha ao criar projeto')
  }
  return resp.json()
}

export async function apiGetProject(id: string): Promise<Project> {
  const resp = await apiFetch(`/projects/${id}`)
  if (!resp.ok) throw new Error('Projeto não encontrado')
  return resp.json()
}

export async function apiUpdateProject(
  id: string,
  updates: { name?: string; description?: string | null; instructions?: string | null },
): Promise<Project> {
  const resp = await apiFetch(`/projects/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(updates),
  })
  if (!resp.ok) throw new Error('Falha ao atualizar projeto')
  return resp.json()
}

export async function apiArchiveProject(id: string): Promise<void> {
  await apiFetch(`/projects/${id}`, { method: 'DELETE' })
}

export async function apiUploadProjectFile(projectId: string, file: File): Promise<ProjectFile> {
  const form = new FormData()
  form.append('file', file)
  const resp = await authorizedFetch(`${API_BASE}/projects/${projectId}/files`, {
    method: 'POST',
    body: form,
  }, {})
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error((err as { detail?: string }).detail ?? 'Falha ao enviar arquivo')
  }
  return resp.json()
}

export async function apiDeleteProjectFile(projectId: string, fileId: string): Promise<void> {
  await apiFetch(`/projects/${projectId}/files/${fileId}`, { method: 'DELETE' })
}

export async function apiGetProjectSessions(projectId: string): Promise<SessionListResponse> {
  const resp = await apiFetch(`/projects/${projectId}/sessions`)
  if (!resp.ok) throw new Error('Falha ao carregar sessões do projeto')
  return resp.json()
}

// ── Workspaces ────────────────────────────────────────────────────────────

export async function apiCreateCodeWorkspace(
  projectId: string,
  opts?: { language_hint?: string; test_command?: string; run_command?: string; build_command?: string },
): Promise<CodeWorkspace> {
  const resp = await apiFetch('/workspaces/code', {
    method: 'POST',
    body: JSON.stringify({ project_id: projectId, ...opts }),
  })
  if (!resp.ok) throw new Error('Falha ao criar CODE workspace')
  return resp.json()
}

export async function apiCreateWorkWorkspace(
  projectId: string,
  opts?: { requires_approval?: boolean; approval_policy?: string },
): Promise<WorkWorkspace> {
  const resp = await apiFetch('/workspaces/work', {
    method: 'POST',
    body: JSON.stringify({ project_id: projectId, ...opts }),
  })
  if (!resp.ok) throw new Error('Falha ao criar WORK workspace')
  return resp.json()
}

export async function apiGetProjectCodeWorkspace(projectId: string): Promise<CodeWorkspace | null> {
  const resp = await apiFetch(`/projects/${projectId}/workspace/code`)
  if (resp.status === 404) return null
  if (!resp.ok) throw new Error('Falha ao carregar CODE workspace')
  return resp.json()
}

export async function apiGetCodeWorkspace(workspaceId: string): Promise<CodeWorkspace> {
  const resp = await apiFetch(`/workspaces/code/${workspaceId}`)
  if (!resp.ok) throw new Error('CODE workspace não encontrado')
  return resp.json()
}

export async function apiExecuteCode(
  workspaceId: string,
  command: string,
  timeout: number,
  onOutput: (data: string) => void,
  onDone: (result: { success: boolean; exit_code: number; duration_ms: number }) => void,
  onError: (message: string) => void,
): Promise<void> {
  let resp: Response
  try {
    resp = await apiFetch(`/workspaces/code/${workspaceId}/execute`, {
      method: 'POST',
      body: JSON.stringify({ command, timeout }),
    })
  } catch {
    onError('Não foi possível conectar ao servidor')
    return
  }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    onError((err as { detail?: string }).detail ?? 'Falha ao executar comando')
    return
  }
  if (!resp.body) return
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''
      for (const line of lines) {
        const trimmed = line.trim()
        if (!trimmed.startsWith('data:')) continue
        const payload = trimmed.slice(5).trim()
        if (payload === '[DONE]') return
        try {
          const ev = JSON.parse(payload) as { type: string; data?: string; success?: boolean; exit_code?: number; duration_ms?: number; message?: string }
          if (ev.type === 'output') onOutput(ev.data ?? '')
          else if (ev.type === 'done') onDone({ success: ev.success ?? false, exit_code: ev.exit_code ?? 0, duration_ms: ev.duration_ms ?? 0 })
          else if (ev.type === 'error') { onError(ev.message ?? 'Erro na execução'); return }
        } catch { /* skip malformed lines */ }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

export async function apiListWorkspaceFiles(workspaceId: string, subpath = ''): Promise<WorkspaceFileEntry[]> {
  const qs = subpath ? `?path=${encodeURIComponent(subpath)}` : ''
  const resp = await apiFetch(`/workspaces/code/${workspaceId}/files${qs}`)
  if (!resp.ok) throw new Error('Falha ao listar arquivos do workspace')
  const data = await resp.json() as { entries: WorkspaceFileEntry[] }
  return data.entries
}

export async function apiReadWorkspaceFile(workspaceId: string, filepath: string): Promise<string> {
  const resp = await apiFetch(`/workspaces/code/${workspaceId}/files/${filepath}`)
  if (!resp.ok) throw new Error('Arquivo não encontrado')
  const data = await resp.json() as { path: string; content: string }
  return data.content
}

export async function apiRunCodeTask(
  workspaceId: string,
  task: string,
  maxIterations: number,
  onStatus: (key: string, message: string) => void,
  onDone: (result: { answer: string; iterations: number; duration_ms: number }) => void,
  onError: (message: string) => void,
): Promise<void> {
  let resp: Response
  try {
    resp = await apiFetch(`/workspaces/code/${workspaceId}/task`, {
      method: 'POST',
      body: JSON.stringify({ task, max_iterations: maxIterations }),
    })
  } catch {
    onError('Não foi possível conectar ao servidor')
    return
  }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    onError((err as { detail?: string }).detail ?? 'Falha ao executar tarefa')
    return
  }
  await parseSSEStream(resp, (event) => {
    if (event.type === 'status') {
      onStatus(event.key ?? '', event.message ?? '')
    } else if (event.type === 'done') {
      onDone({
        answer: (event as unknown as Record<string, unknown>).answer as string ?? '',
        iterations: (event as unknown as Record<string, unknown>).iterations as number ?? 0,
        duration_ms: (event as unknown as Record<string, unknown>).duration_ms as number ?? 0,
      })
    } else if (event.type === 'error') {
      onError(event.message ?? 'Erro na tarefa')
      return true
    }
  })
}

// ── WORK tasks ───────────────────────────────────────────────────────────

/** Alias — see types.ts's ApprovalRequiredEvent, the shared shape now
 * also used by TRINITY AGENT's own approval gate. Kept under this name
 * for backward compatibility with the WORK page's existing import. */
export type WorkApprovalEvent = ApprovalRequiredEvent

export async function apiRunWorkTask(
  workspaceId: string,
  task: string,
  maxIterations: number,
  onStatus: (key: string, message: string) => void,
  onApprovalRequired: (event: WorkApprovalEvent) => void,
  onDone: (result: { answer: string; iterations: number; duration_ms: number }) => void,
  onError: (message: string) => void,
): Promise<void> {
  let resp: Response
  try {
    resp = await apiFetch(`/workspaces/work/${workspaceId}/execute`, {
      method: 'POST',
      body: JSON.stringify({ task, max_iterations: maxIterations }),
    })
  } catch {
    onError('Não foi possível conectar ao servidor')
    return
  }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    onError((err as { detail?: string }).detail ?? 'Falha ao executar tarefa')
    return
  }
  await parseSSEStream(resp, (event) => {
    if (event.type === 'status') {
      onStatus(event.key ?? '', event.message ?? '')
    } else if (event.type === 'approval_required') {
      onApprovalRequired(event as unknown as WorkApprovalEvent)
    } else if (event.type === 'done') {
      const ev = event as unknown as Record<string, unknown>
      onDone({
        answer: ev.answer as string ?? '',
        iterations: ev.iterations as number ?? 0,
        duration_ms: ev.duration_ms as number ?? 0,
      })
    } else if (event.type === 'error') {
      onError(event.message ?? 'Erro na tarefa')
      return true
    }
  })
}

export async function apiApproveWorkAction(workspaceId: string, actionId: string): Promise<void> {
  const resp = await apiFetch(`/workspaces/work/${workspaceId}/approve`, {
    method: 'POST',
    body: JSON.stringify({ action_id: actionId }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error((err as { detail?: string }).detail ?? 'Falha ao aprovar ação')
  }
}

export async function apiRejectWorkAction(workspaceId: string, actionId: string): Promise<void> {
  const resp = await apiFetch(`/workspaces/work/${workspaceId}/reject`, {
    method: 'POST',
    body: JSON.stringify({ action_id: actionId }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error((err as { detail?: string }).detail ?? 'Falha ao rejeitar ação')
  }
}

export async function apiGetWorkWorkspace(workspaceId: string): Promise<WorkWorkspace> {
  const resp = await apiFetch(`/workspaces/work/${workspaceId}`)
  if (!resp.ok) throw new Error('WORK workspace não encontrado')
  return resp.json()
}

// ── Skills ────────────────────────────────────────────────────────────────

export interface SkillListResponse { skills: Skill[]; total: number }

export async function apiListSkills(): Promise<SkillListResponse> {
  const resp = await apiFetch('/skills')
  if (!resp.ok) throw new Error('Falha ao carregar skills')
  return resp.json()
}

export async function apiCreateSkill(
  name: string,
  toolIds: string[],
  opts?: { description?: string; instructions?: string; icon_emoji?: string },
): Promise<Skill> {
  const resp = await apiFetch('/skills', {
    method: 'POST',
    body: JSON.stringify({ name, tool_ids: toolIds, ...opts }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error((err as { detail?: string }).detail ?? 'Falha ao criar skill')
  }
  return resp.json()
}

export async function apiUpdateSkill(
  id: string,
  updates: { name?: string; description?: string; instructions?: string; tool_ids?: string[]; icon_emoji?: string },
): Promise<Skill> {
  const resp = await apiFetch(`/skills/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(updates),
  })
  if (!resp.ok) throw new Error('Falha ao atualizar skill')
  return resp.json()
}

export async function apiDeleteSkill(id: string): Promise<void> {
  await apiFetch(`/skills/${id}`, { method: 'DELETE' })
}

// ── Agent Definitions ──────────────────────────────────────────────────────

export interface AgentDefinitionListResponse { agents: AgentDefinition[]; total: number }

export async function apiListAgents(): Promise<AgentDefinitionListResponse> {
  const resp = await apiFetch('/agents')
  if (!resp.ok) throw new Error('Falha ao carregar agentes')
  return resp.json()
}

export async function apiCreateAgent(
  name: string,
  opts?: {
    description?: string
    avatar_emoji?: string
    system_instructions?: string
    skill_ids?: string[]
    model_preference?: 'power' | 'balanced' | 'fast'
  },
): Promise<AgentDefinition> {
  const resp = await apiFetch('/agents', {
    method: 'POST',
    body: JSON.stringify({ name, skill_ids: [], model_preference: 'balanced', ...opts }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error((err as { detail?: string }).detail ?? 'Falha ao criar agente')
  }
  return resp.json()
}

export async function apiGetAgent(id: string): Promise<AgentDefinition> {
  const resp = await apiFetch(`/agents/${id}`)
  if (!resp.ok) throw new Error('Agente não encontrado')
  return resp.json()
}

export async function apiUpdateAgent(
  id: string,
  updates: {
    name?: string
    description?: string | null
    avatar_emoji?: string | null
    system_instructions?: string | null
    skill_ids?: string[]
    model_preference?: 'power' | 'balanced' | 'fast'
  },
): Promise<AgentDefinition> {
  const resp = await apiFetch(`/agents/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(updates),
  })
  if (!resp.ok) throw new Error('Falha ao atualizar agente')
  return resp.json()
}

export async function apiArchiveAgent(id: string): Promise<void> {
  await apiFetch(`/agents/${id}`, { method: 'DELETE' })
}

export async function apiAddAgentToProject(projectId: string, agentId: string): Promise<void> {
  await apiFetch(`/projects/${projectId}/agents/${agentId}`, { method: 'POST' })
}

export async function apiRemoveAgentFromProject(projectId: string, agentId: string): Promise<void> {
  await apiFetch(`/projects/${projectId}/agents/${agentId}`, { method: 'DELETE' })
}

export async function apiListProjectAgents(projectId: string): Promise<AgentDefinition[]> {
  const resp = await apiFetch(`/projects/${projectId}/agents`)
  if (!resp.ok) throw new Error('Falha ao carregar agentes do projeto')
  return resp.json()
}

// ── Memories ──────────────────────────────────────────────────────────────

export async function apiListMemories(): Promise<Memory[]> {
  const resp = await apiFetch('/users/me/memories')
  if (!resp.ok) throw new Error('Falha ao carregar memórias')
  return resp.json()
}

export async function apiCreateMemory(content: string): Promise<Memory> {
  const resp = await apiFetch('/users/me/memories', {
    method: 'POST',
    body: JSON.stringify({ content }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    const detail = (err as { detail?: unknown }).detail
    throw new Error(typeof detail === 'string' ? detail : 'Falha ao salvar memória')
  }
  return resp.json()
}

export async function apiDeleteMemory(id: string): Promise<void> {
  const resp = await apiFetch(`/users/me/memories/${id}`, { method: 'DELETE' })
  if (!resp.ok) throw new Error('Falha ao remover memória')
}

// ── Billing ───────────────────────────────────────────────────────────────

export async function apiGetCreditSummary(): Promise<CreditSummary> {
  const resp = await apiFetch('/billing/balance')
  if (!resp.ok) throw new Error('Falha ao carregar créditos')
  return resp.json()
}

export async function apiGetCreditBalance(currency: string): Promise<CreditBalance> {
  const resp = await apiFetch(`/billing/balance/${currency.toUpperCase()}`)
  if (!resp.ok) throw new Error('Falha ao carregar saldo')
  return resp.json()
}

export async function apiListOrders(opts?: { status?: string; limit?: number; offset?: number }): Promise<PaymentOrderListResponse> {
  const params = new URLSearchParams()
  if (opts?.status) params.set('status', opts.status)
  if (opts?.limit !== undefined) params.set('limit', String(opts.limit))
  if (opts?.offset !== undefined) params.set('offset', String(opts.offset))
  const qs = params.toString()
  const resp = await apiFetch(`/billing/orders${qs ? `?${qs}` : ''}`)
  if (!resp.ok) throw new Error('Falha ao carregar pedidos')
  return resp.json()
}

export async function apiGetOrder(id: string): Promise<PaymentOrder> {
  const resp = await apiFetch(`/billing/orders/${id}`)
  if (!resp.ok) throw new Error('Pedido não encontrado')
  return resp.json()
}

// ── Commercial Checkout V1 ───────────────────────────────────────────────

/** Server-side price catalog — never hardcode plan prices in the frontend,
 * same convention already established for entitlement limits. */
export async function apiListPlanPrices(): Promise<PlanPrice[]> {
  const resp = await apiFetch('/billing/plans')
  if (!resp.ok) throw new Error('Falha ao carregar preços dos planos')
  return resp.json()
}

/** Empty in any environment without real payment credentials configured
 * — the honest, expected answer today. Used to decide whether to show a
 * real checkout flow or fall back to the manual/email upgrade request. */
export async function apiListPaymentProviders(): Promise<PaymentProvidersResponse> {
  const resp = await apiFetch('/billing/providers')
  if (!resp.ok) throw new Error('Falha ao carregar provedores de pagamento')
  return resp.json()
}

export async function apiCreateCheckout(plan: string, provider: string, idempotencyKey: string): Promise<CheckoutResponse> {
  const resp = await apiFetch('/billing/checkout', {
    method: 'POST',
    body: JSON.stringify({ plan, provider, idempotency_key: idempotencyKey }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error(extractErrorMessage(err, resp.status))
  }
  return resp.json()
}

/** Admin/test-only manual confirmation — NEVER called from the real
 * checkout UI (see plans/page.tsx). The backend itself refuses this for
 * any order whose provider has a real adapter registered — only usable
 * for a local-only order (no payment provider configured), exactly the
 * "mecanismo de teste/admin" the mission allows, never a substitute for
 * a real webhook in production. */
export async function apiConfirmOrder(id: string): Promise<PaymentOrder> {
  const resp = await apiFetch(`/billing/orders/${id}/confirm`, { method: 'POST' })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error(extractErrorMessage(err, resp.status))
  }
  return resp.json()
}

// ── Voice (STT/TTS) ──────────────────────────────────────────────────────
// The API key never touches the frontend — every provider call goes through
// these backend routes. See packages/core/src/api/routes/voice.py.

async function readErrorDetail(resp: Response, fallback: string): Promise<string> {
  const err = await resp.json().catch(() => ({}))
  const detail = (err as { detail?: unknown }).detail
  return typeof detail === 'string' ? detail : fallback
}

/** Upload a recorded audio blob and get back the transcribed text. */
export async function apiTranscribeAudio(audio: Blob, filename = 'clip.webm'): Promise<VoiceTranscription> {
  const form = new FormData()
  form.append('file', audio, filename)
  const resp = await authorizedFetch(`${API_BASE}/voice/transcribe`, {
    method: 'POST',
    body: form,
  }, {})
  if (!resp.ok) {
    throw new Error(await readErrorDetail(resp, 'Falha ao transcrever áudio'))
  }
  return resp.json()
}

/** Synthesize text to speech and return a playable audio Blob. */
export async function apiSynthesizeSpeech(text: string, voiceId?: string): Promise<Blob> {
  const resp = await apiFetch('/voice/synthesize', {
    method: 'POST',
    body: JSON.stringify({ text, voice_id: voiceId ?? null }),
  })
  if (!resp.ok) {
    throw new Error(await readErrorDetail(resp, 'Falha ao sintetizar áudio'))
  }
  return resp.blob()
}

// ── Agent SSE ─────────────────────────────────────────────────────────────

export async function apiStreamAgent(
  sessionId: string,
  content: string,
  onStep: (step: AgentStep) => void,
  onDone: (event: AgentDoneEvent) => void,
  onError: (message: string) => void,
  onStatus?: (message: string) => void,
  onApprovalRequired?: (event: WorkApprovalEvent) => void,
): Promise<void> {
  let resp: Response
  try {
    resp = await apiFetch(`/sessions/${sessionId}/agent`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    })
  } catch {
    onError('Não foi possível conectar ao TRINDATRON (verifique se o servidor está rodando)')
    return
  }

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    onError(extractErrorMessage(err, resp.status))
    return
  }

  await parseSSEStream(resp, (event) => {
    if (event.type === 'status') {
      onStatus?.(event.key ?? event.message ?? '')
    } else if (event.type === 'tool_call' || event.type === 'tool_result') {
      onStep(event as unknown as AgentStep)
    } else if (event.type === 'approval_required') {
      onApprovalRequired?.(event as unknown as WorkApprovalEvent)
    } else if (event.type === 'done') {
      onDone(event as unknown as AgentDoneEvent)
    } else if (event.type === 'error') {
      onError(event.message ?? 'Erro no agente')
      return true
    }
  })
}

/** Resolves a CONFIRM-level agent tool call's approval gate (write_file/
 * execute_shell) — mirrors apiApproveWorkAction/apiRejectWorkAction,
 * session-scoped instead of workspace-scoped. */
export async function apiApproveAgentAction(sessionId: string, actionId: string): Promise<void> {
  const resp = await apiFetch(`/sessions/${sessionId}/agent/approve`, {
    method: 'POST',
    body: JSON.stringify({ action_id: actionId }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error((err as { detail?: string }).detail ?? 'Falha ao aprovar ação')
  }
}

export async function apiRejectAgentAction(sessionId: string, actionId: string): Promise<void> {
  const resp = await apiFetch(`/sessions/${sessionId}/agent/reject`, {
    method: 'POST',
    body: JSON.stringify({ action_id: actionId }),
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    throw new Error((err as { detail?: string }).detail ?? 'Falha ao rejeitar ação')
  }
}

// ── Unified message endpoint (Capability Router) ───────────────────────────
// Calls POST /sessions/{id}/message — backend classifies and dispatches to
// the correct pipeline automatically. Handles both chat and agent event shapes.

export async function apiSendMessage(
  sessionId: string,
  content: string,
  onText: (chunk: string) => void,
  onDone: (event: DoneEvent | AgentDoneEvent) => void,
  onError: (message: string) => void,
  onStatus?: (keyOrMessage: string) => void,
  onAgentStep?: (step: AgentStep) => void,
  onApprovalRequired?: (event: WorkApprovalEvent) => void,
  onArtifact?: (artifact: MediaArtifactEvent) => void,
): Promise<void> {
  let resp: Response
  try {
    resp = await apiFetch(`/sessions/${sessionId}/message`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    })
  } catch {
    onError('Não foi possível conectar ao TRINDATRON (verifique se o servidor está rodando)')
    return
  }

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    onError(extractErrorMessage(err, resp.status))
    return
  }

  await parseSSEStream(resp, (event) => {
    if (event.type === 'status') {
      onStatus?.(event.key ?? event.message ?? '')
    } else if (event.type === 'text' && event.content) {
      onText(event.content)
    } else if (event.type === 'tool_call' || event.type === 'tool_result') {
      onAgentStep?.(event as unknown as AgentStep)
    } else if (event.type === 'approval_required') {
      onApprovalRequired?.(event as unknown as WorkApprovalEvent)
    } else if (event.type === 'artifact' && event.artifact && typeof event.artifact.id === 'string') {
      onArtifact?.({ id: event.artifact.id, kind: String(event.artifact.kind), status: String(event.artifact.status) })
    } else if (event.type === 'done') {
      // Agent pipeline emits done with `answer`; chat pipeline does not.
      if (event.answer !== undefined) {
        // Agent done: emit final answer text then call done handler.
        onText(event.answer)
      }
      onDone(event as unknown as DoneEvent | AgentDoneEvent)
    } else if (event.type === 'error') {
      onError(event.message ?? 'Erro no stream')
      return true
    }
  })
}

// ── Capabilities ──────────────────────────────────────────────────────────

export interface CapabilityState {
  available: boolean
  daily_quota?: { limit: number; remaining: number; resets_at: string }
}

/** What this account can do right now, by abstract name (image_generation, video_generation, image_understanding…). */
export type Capabilities = Record<string, CapabilityState>

export async function apiGetCapabilities(): Promise<Capabilities> {
  const resp = await apiFetch('/users/me/capabilities')
  if (!resp.ok) throw new Error(extractErrorMessage(await resp.json().catch(() => ({})), resp.status))
  return ((await resp.json()) as { capabilities: Capabilities }).capabilities
}

// ── Conversation attachments ──────────────────────────────────────────────

export interface SessionAttachment {
  id: string
  session_id: string
  name: string
  mime_type: string
  size_bytes: number
  kind: string
  project_file_id: string | null
  /** Whether TRINDATRON can read the content (documents, and images it has read). */
  readable: boolean
  created_at: string
  content_path: string
}

const ATTACHMENT_ERRORS: Record<string, string> = {
  too_large: 'O arquivo é maior do que o limite de 10 MB.',
  too_many: 'Esta conversa já tem o máximo de arquivos anexados.',
  invalid: 'Este arquivo não pode ser anexado.',
  unavailable: 'Anexar arquivos não está disponível agora.',
  not_found: 'Conversa não encontrada.',
}

async function attachmentError(resp: Response): Promise<string> {
  const body = await resp.json().catch(() => ({}))
  const detail = (body as { detail?: unknown }).detail
  const code = detail && typeof detail === 'object' ? (detail as { error?: unknown }).error : null
  if (typeof code === 'string' && ATTACHMENT_ERRORS[code]) return ATTACHMENT_ERRORS[code]
  return extractErrorMessage(body, resp.status)
}

export async function apiListAttachments(sessionId: string): Promise<SessionAttachment[]> {
  const resp = await apiFetch(`/sessions/${sessionId}/attachments`)
  if (!resp.ok) throw new Error(await attachmentError(resp))
  return ((await resp.json()) as { attachments: SessionAttachment[] }).attachments
}

/** Multipart upload: no JSON content type, so the browser sets the boundary. */
export async function apiAttachFile(sessionId: string, file: File): Promise<SessionAttachment> {
  const form = new FormData()
  form.append('file', file, file.name)
  const resp = await authorizedFetch(`${API_BASE}/sessions/${sessionId}/attachments`, { method: 'POST', body: form }, {})
  if (!resp.ok) throw new Error(await attachmentError(resp))
  return resp.json()
}

export async function apiRemoveAttachment(sessionId: string, attachmentId: string): Promise<void> {
  const resp = await apiFetch(`/sessions/${sessionId}/attachments/${attachmentId}`, { method: 'DELETE' })
  if (!resp.ok && resp.status !== 404) throw new Error(await attachmentError(resp))
}

// ── Creations ─────────────────────────────────────────────────────────────

export interface MediaArtifact {
  id: string
  kind: string
  content_type: string
  width: number | null
  height: number | null
  duration_seconds: number | null
  content_path: string
  download_path: string
}

export interface MediaJob {
  id: string
  kind: 'image' | 'video' | 'avatar'
  status: 'queued' | 'running' | 'completed' | 'failed'
  progress: number | null
  error: string | null
  created_at: string
  completed_at: string | null
  artifact: MediaArtifact | null
}

export async function apiGetMediaJob(jobId: string): Promise<MediaJob> {
  const resp = await apiFetch(`/media/jobs/${jobId}`)
  if (!resp.ok) throw new Error(extractErrorMessage(await resp.json().catch(() => ({})), resp.status))
  return resp.json()
}

export interface MediaLink {
  /** Short-lived; null where the storage has no signed links (local development). */
  url: string | null
  download_url: string | null
  expires_in: number | null
  content_type: string
  filename: string
}

/**
 * Links a media element can load directly. Reading the file with fetch() follows a redirect to
 * object storage, which the browser blocks without CORS on the bucket; <video>/<img> and a plain
 * link do not need it.
 */
export async function apiGetMediaLink(artifactId: string): Promise<MediaLink> {
  const resp = await apiFetch(`/media/artifacts/${artifactId}/link`)
  if (!resp.ok) throw new Error(extractErrorMessage(await resp.json().catch(() => ({})), resp.status))
  return resp.json()
}

/** A created file, read with the session: creations are private to their owner. */
export async function apiFetchMediaBlob(path: string): Promise<Blob> {
  if (!path.startsWith('/media/')) throw new Error('Arquivo inválido.')
  const resp = await authorizedFetch(`${API_BASE}${path}`, { method: 'GET' }, {})
  if (!resp.ok) throw new Error(`Erro ${resp.status}`)
  return resp.blob()
}

// ── Settings: preferences, integrations, data, devices, scheduled tasks ─────
// Every value below comes from the Core. Nothing here names a provider or a model.

async function jsonOrThrow<T>(resp: Response): Promise<T> {
  if (!resp.ok) throw new Error(extractErrorMessage(await resp.json().catch(() => ({})), resp.status))
  return resp.json() as Promise<T>
}

export type LanguagePreference = 'auto' | 'pt' | 'en' | 'es'

export interface Preferences {
  language: LanguagePreference
  response_style: 'balanced' | 'concise' | 'detailed'
  /** Enter sends; off, Ctrl/Cmd+Enter sends (components/chat/composer.tsx). */
  send_on_enter: boolean
  /** Read each finished answer aloud (app/(app)/chat/[sessionId]/page.tsx → message-bubble.tsx). */
  voice_auto_play: boolean
  memory_enabled: boolean
  disabled_plugins: string[]
}

export async function apiGetPreferences(): Promise<Preferences> {
  return jsonOrThrow(await apiFetch('/users/me/preferences'))
}

export async function apiUpdatePreferences(changes: Partial<Preferences>): Promise<Preferences> {
  return jsonOrThrow(await apiFetch('/users/me/preferences', { method: 'PATCH', body: JSON.stringify(changes) }))
}

export type IntegrationStatus = 'available' | 'disabled' | 'plan' | 'unavailable' | 'not_available'

export interface PluginState {
  id: string
  status: IntegrationStatus
  enabled: boolean
  permissions: string[]
  approval_required: string[]
  scope: string | null
}

export interface ConnectorState {
  id: string
  status: IntegrationStatus
  connected: boolean
}

export interface Integrations {
  plugins: PluginState[]
  connectors: ConnectorState[]
  extensions: unknown[]
}

export async function apiGetIntegrations(): Promise<Integrations> {
  return jsonOrThrow(await apiFetch('/users/me/integrations'))
}

export async function apiSetPluginEnabled(pluginId: string, enabled: boolean): Promise<PluginState> {
  return jsonOrThrow(await apiFetch(`/users/me/integrations/plugins/${encodeURIComponent(pluginId)}`, {
    method: 'PATCH', body: JSON.stringify({ enabled }),
  }))
}

export async function apiExportMyData(): Promise<Blob> {
  const resp = await apiFetch('/users/me/export')
  if (!resp.ok) throw new Error(extractErrorMessage(await resp.json().catch(() => ({})), resp.status))
  return resp.blob()
}

export async function apiClearMemories(): Promise<{ deleted: number }> {
  return jsonOrThrow(await apiFetch('/users/me/memories', { method: 'DELETE' }))
}

export interface DeviceSession {
  id: string
  client: 'web' | 'desktop' | 'mobile' | string
  user_agent: string | null
  started_at: string
  last_used_at: string
  expires_at: string
  current: boolean
}

export async function apiListDeviceSessions(): Promise<DeviceSession[]> {
  const body = await jsonOrThrow<{ sessions: DeviceSession[] }>(await apiFetch('/auth/sessions', { credentials: 'include' }))
  return body.sessions
}

export async function apiRevokeDeviceSession(id: string): Promise<void> {
  const resp = await apiFetch(`/auth/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' })
  if (!resp.ok && resp.status !== 404) throw new Error(`Erro ${resp.status}`)
}

export async function apiSignOutEverywhere(): Promise<void> {
  const resp = await apiFetch('/auth/sessions/revoke-all', { method: 'POST', credentials: 'include' })
  if (!resp.ok) throw new Error(`Erro ${resp.status}`)
}

export type RecurrenceKind = 'hourly' | 'daily' | 'weekly'

export interface Recurrence {
  kind: RecurrenceKind
  time?: string
  weekday?: number
  timezone?: string
}

export interface ScheduledTask {
  id: string
  title: string
  instruction: string
  recurrence: Recurrence
  is_active: boolean
  next_run_at: string | null
  last_run_at: string | null
  last_outcome: string | null
  last_run: { job_id: string; status: string; completed_at: string | null; excerpt: string | null } | null
  created_at: string | null
}

export async function apiListScheduledTasks(): Promise<ScheduledTask[]> {
  return (await jsonOrThrow<{ tasks: ScheduledTask[] }>(await apiFetch('/scheduled-tasks'))).tasks
}

export async function apiCreateScheduledTask(body: { title: string; instruction: string; recurrence: Recurrence }): Promise<ScheduledTask> {
  return jsonOrThrow(await apiFetch('/scheduled-tasks', { method: 'POST', body: JSON.stringify(body) }))
}

export async function apiUpdateScheduledTask(id: string, changes: Partial<Pick<ScheduledTask, 'title' | 'instruction' | 'is_active'>> & { recurrence?: Recurrence }): Promise<ScheduledTask> {
  return jsonOrThrow(await apiFetch(`/scheduled-tasks/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(changes) }))
}

export async function apiDeleteScheduledTask(id: string): Promise<void> {
  const resp = await apiFetch(`/scheduled-tasks/${encodeURIComponent(id)}`, { method: 'DELETE' })
  if (!resp.ok) throw new Error(extractErrorMessage(await resp.json().catch(() => ({})), resp.status))
}

export async function apiRunScheduledTask(id: string): Promise<{ job_id: string; status: string }> {
  return jsonOrThrow(await apiFetch(`/scheduled-tasks/${encodeURIComponent(id)}/run`, { method: 'POST' }))
}

export interface MatrixEntry {
  available: boolean
  status: 'available' | 'plan' | 'unavailable' | 'not_integrated'
  inputs: string[]
  outputs: string[]
  health: string
  fallback: boolean
  scope?: string
  limits?: Record<string, unknown>
}

export interface MySubscription {
  id: string
  plan: string
  status: string
  amount: string
  currency: string
  payment_method: string
  current_period_end: string | null
  next_billing_at: string | null
  cancelled_at: string | null
}

export async function apiListMySubscriptions(): Promise<MySubscription[]> {
  return (await jsonOrThrow<{ subscriptions: MySubscription[] }>(await apiFetch('/users/me/subscriptions'))).subscriptions
}

export async function apiCancelSubscription(id: string): Promise<void> {
  const resp = await apiFetch(`/billing/subscriptions/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: '{}' })
  if (!resp.ok) throw new Error(extractErrorMessage(await resp.json().catch(() => ({})), resp.status))
}

/** The capability matrix: the single source of truth for what this account can do right now. */
export async function apiGetCapabilityMatrix(): Promise<Record<string, MatrixEntry>> {
  const body = await jsonOrThrow<{ matrix?: Record<string, MatrixEntry> }>(await apiFetch('/users/me/capabilities'))
  return body.matrix ?? {}
}
