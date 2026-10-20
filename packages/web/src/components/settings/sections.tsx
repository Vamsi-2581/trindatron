'use client'

/**
 * Settings sections. Every value shown comes from the TRINDATRON Core
 * (preferences, integrations, capability matrix, capacity, sessions, billing,
 * jobs). A control exists only when the Core honours it; where something is not
 * integrated yet, the section says exactly that instead of pretending.
 * Nothing here names a provider, a model, a cost or an internal unit.
 */
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { useCallback, useEffect, useState, type ComponentType, type FormEvent, type ReactNode } from 'react'
import {
  apiCancelSubscription,
  apiChangePassword,
  apiClearMemories,
  apiExportMyData,
  apiGetCapacity,
  apiGetCapabilityMatrix,
  apiGetIntegrations,
  apiGetMe,
  apiGetPreferences,
  apiListDeviceSessions,
  apiListExecutionJobs,
  apiListMemories,
  apiListMySubscriptions,
  apiListOrders,
  apiListSecurityEvents,
  apiListSkills,
  apiResendVerification,
  apiRevokeDeviceSession,
  apiSetPluginEnabled,
  apiSignOutEverywhere,
  apiUpdatePreferences,
  type DeviceSession,
  type Integrations,
  type IntegrationStatus,
  type MatrixEntry,
  type MySubscription,
  type Preferences,
  type SecurityEvent,
} from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { ScheduledTasks } from '@/components/scheduled/scheduled-tasks'
import { useAppStore } from '@/store/useAppStore'
import type { Capacity, ExecutionJob, PaymentOrder, Skill } from '@/types'

// ── Shared pieces ────────────────────────────────────────────────────────────

function useLoad<T>(load: () => Promise<T>): { data: T | null; error: string | null; reload: () => void } {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)
  useEffect(() => {
    let cancelled = false
    load()
      .then((value) => { if (!cancelled) { setData(value); setError(null) } })
      .catch((err: unknown) => { if (!cancelled) setError(err instanceof Error ? err.message : 'Não foi possível carregar.') })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick])
  return { data, error, reload: () => setTick((t) => t + 1) }
}

function Header({ title, description }: { title: string; description: string }) {
  return (
    <div className="mb-6">
      <h1 className="text-lg font-semibold text-text-primary">{title}</h1>
      <p className="text-sm text-text-muted mt-1">{description}</p>
    </div>
  )
}

function Block({ title, children, footer }: { title: string; children: ReactNode; footer?: ReactNode }) {
  return (
    <Card className="mb-4">
      <Card.Header><p className="text-sm font-medium text-text-primary">{title}</p></Card.Header>
      <Card.Body>{children}</Card.Body>
      {footer ? <Card.Footer>{footer}</Card.Footer> : null}
    </Card>
  )
}

function Loading({ error }: { error: string | null }) {
  return <p className={`text-xs ${error ? 'text-error' : 'text-text-muted'}`}>{error ?? 'Carregando…'}</p>
}

const STATUS_LABEL: Record<string, string> = {
  available: 'Ativo',
  disabled: 'Desativado',
  plan: 'Não incluído no plano',
  unavailable: 'Indisponível agora',
  not_available: 'Ainda não disponível',
  not_integrated: 'Ainda não disponível',
}

function StatusBadge({ status }: { status: IntegrationStatus | MatrixEntry['status'] | string }) {
  const variant = status === 'available' ? 'success' : status === 'plan' ? 'accent' : status === 'unavailable' ? 'warning' : 'muted'
  return <Badge variant={variant}>{STATUS_LABEL[status] ?? status}</Badge>
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' })
}

function Toggle({ checked, onChange, disabled, label }: { checked: boolean; onChange: (next: boolean) => void; disabled?: boolean; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={[
        'relative w-9 h-5 rounded-full transition-colors shrink-0 disabled:opacity-40',
        checked ? 'bg-accent' : 'bg-surface-3',
      ].join(' ')}
    >
      <span className={['absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all', checked ? 'left-4' : 'left-0.5'].join(' ')} />
    </button>
  )
}

function usePreferences() {
  const { data, error, reload } = useLoad(apiGetPreferences)
  const [saving, setSaving] = useState(false)
  const [local, setLocal] = useState<Preferences | null>(null)
  useEffect(() => { if (data) setLocal(data) }, [data])
  const save = useCallback(async (changes: Partial<Preferences>) => {
    setSaving(true)
    try {
      setLocal(await apiUpdatePreferences(changes))
    } finally {
      setSaving(false)
    }
  }, [])
  return { preferences: local, error, saving, save, reload }
}

const CAPABILITY_LABELS: Record<string, string> = {
  text: 'Conversa e escrita',
  vision: 'Entender imagens',
  image_generation: 'Criar imagens',
  image_editing: 'Editar imagens',
  video_generation: 'Criar vídeos',
  video_editing: 'Editar vídeos',
  audio_input: 'Falar com o TRINDATRON',
  audio_output: 'Ouvir respostas',
  voice_conversation: 'Conversa por voz',
  transcription: 'Transcrição',
  image_to_video: 'Animar imagens',
  video_to_video: 'Transformar vídeos',
  avatar_generation: 'Avatares',
  agent: 'Agentes (tarefas em várias etapas)',
  browser_control: 'Usar o navegador',
  computer_control: 'Usar um computador virtual',
  file_analysis: 'Analisar arquivos',
  document_generation: 'Criar apresentações',
  code_execution: 'Executar código',
}

function MatrixList({ matrix, keys }: { matrix: Record<string, MatrixEntry>; keys: string[] }) {
  return (
    <ul className="divide-y divide-border">
      {keys.filter((key) => matrix[key]).map((key) => {
        const entry = matrix[key]
        const quota = entry.limits?.daily_quota as { limit: number; remaining: number } | undefined
        return (
          <li key={key} className="flex items-center justify-between py-2.5 gap-3">
            <div>
              <p className="text-sm text-text-primary">{CAPABILITY_LABELS[key] ?? key}</p>
              {quota && entry.available ? (
                <p className="text-xs text-text-muted">{quota.remaining} de {quota.limit} disponíveis hoje</p>
              ) : null}
            </div>
            <StatusBadge status={entry.status} />
          </li>
        )
      })}
    </ul>
  )
}

// ── Personalizar ─────────────────────────────────────────────────────────────

function SkillsSection() {
  const matrix = useLoad(apiGetCapabilityMatrix)
  const skills = useLoad(apiListSkills)
  const available = matrix.data ? Object.keys(matrix.data).filter((key) => matrix.data?.[key]?.available) : []
  const custom: Skill[] = (skills.data as { skills?: Skill[] } | null)?.skills ?? []
  return (
    <>
      <Header title="Habilidades" description="O que o TRINDATRON sabe fazer por você agora, e as instruções que você ensinou a ele." />
      <Block title="Pronto para usar">
        {matrix.data ? (
          <div className="flex flex-wrap gap-2">
            {available.map((key) => <Badge key={key} variant="default">{CAPABILITY_LABELS[key] ?? key}</Badge>)}
          </div>
        ) : <Loading error={matrix.error} />}
      </Block>
      <Block title="Suas habilidades" footer={<Link href="/agents" className="text-xs text-accent-light hover:underline">Usar em um agente</Link>}>
        {skills.data ? (
          custom.filter((skill) => !skill.is_builtin).length ? (
            <ul className="space-y-2">
              {custom.filter((skill) => !skill.is_builtin).map((skill) => (
                <li key={skill.id} className="text-sm text-text-primary">{skill.icon_emoji ?? '•'} {skill.name}
                  {skill.description ? <span className="block text-xs text-text-muted">{skill.description}</span> : null}
                </li>
              ))}
            </ul>
          ) : <p className="text-xs text-text-muted">Você ainda não criou habilidades próprias.</p>
        ) : <Loading error={skills.error} />}
      </Block>
    </>
  )
}

const CONNECTOR_LABELS: Record<string, { name: string; description: string }> = {
  storage: { name: 'Armazenamento', description: 'Seus arquivos em serviços de nuvem.' },
  calendar: { name: 'Calendário', description: 'Seus compromissos e agendas.' },
  email: { name: 'Email', description: 'Ler e organizar suas mensagens.' },
  search: { name: 'Busca externa', description: 'Fontes de pesquisa conectadas à sua conta.' },
}

function ConnectorsSection() {
  const { data, error } = useLoad(apiGetIntegrations)
  return (
    <>
      <Header title="Conectores" description="Ligações com serviços e fontes de dados fora do TRINDATRON, sempre com a sua autorização." />
      {data ? (
        <Block title="Serviços">
          <ul className="divide-y divide-border">
            {data.connectors.map((connector) => (
              <li key={connector.id} className="flex items-center justify-between py-2.5 gap-3">
                <div>
                  <p className="text-sm text-text-primary">{CONNECTOR_LABELS[connector.id]?.name ?? connector.id}</p>
                  <p className="text-xs text-text-muted">{CONNECTOR_LABELS[connector.id]?.description}</p>
                </div>
                <StatusBadge status={connector.status} />
              </li>
            ))}
          </ul>
          <p className="text-xs text-text-muted mt-3">
            Nenhum conector externo está disponível ainda. Quando estiver, você verá aqui o que ele pode acessar antes de autorizar.
          </p>
        </Block>
      ) : <Loading error={error} />}
    </>
  )
}

const PLUGIN_LABELS: Record<string, { name: string; description: string }> = {
  web_search: { name: 'Pesquisa na web', description: 'Procura informações públicas e atuais.' },
  browser: { name: 'Navegador', description: 'Abre sites, lê páginas, encontra opções, clica e preenche.' },
  files: { name: 'Arquivos', description: 'Lê e escreve arquivos no espaço de trabalho da conversa.' },
  code_execution: { name: 'Execução de código', description: 'Roda comandos num ambiente isolado.' },
  computer: { name: 'Computador virtual', description: 'Vê a tela e usa um computador virtual isolado — não o seu dispositivo.' },
}

const PERMISSION_LABELS: Record<string, string> = {
  read_public_web: 'ler a web pública',
  open_websites: 'abrir sites',
  read_pages: 'ler páginas',
  click_and_fill: 'clicar e preencher',
  read_workspace_files: 'ler arquivos do espaço de trabalho',
  write_workspace_files: 'escrever arquivos do espaço de trabalho',
  run_commands_in_sandbox: 'executar comandos em ambiente isolado',
  see_virtual_screen: 'ver a tela virtual',
  control_virtual_computer: 'controlar o computador virtual',
}

const ACTION_LABELS: Record<string, string> = {
  browser_click: 'clicar', browser_fill: 'preencher', write_file: 'escrever arquivos', execute_shell: 'executar comandos',
  desktop_screenshot: 'ver a tela', desktop_click: 'clicar', desktop_double_click: 'clicar duas vezes',
  desktop_right_click: 'clique direito', desktop_type: 'digitar', desktop_key: 'teclas', desktop_scroll: 'rolar',
  desktop_drag: 'arrastar', desktop_move: 'mover', desktop_open: 'abrir aplicativos', desktop_get_windows: 'listar janelas',
}

function PluginsList({ integrations, onToggle, only }: { integrations: Integrations; onToggle: (id: string, enabled: boolean) => void; only?: string[] }) {
  return (
    <ul className="divide-y divide-border">
      {integrations.plugins.filter((plugin) => !only || only.includes(plugin.id)).map((plugin) => {
        const reachable = plugin.status === 'available' || plugin.status === 'disabled'
        return (
          <li key={plugin.id} className="py-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-sm text-text-primary">{PLUGIN_LABELS[plugin.id]?.name ?? plugin.id}</p>
                <p className="text-xs text-text-muted">{PLUGIN_LABELS[plugin.id]?.description}</p>
                <p className="text-xs text-text-muted mt-1">Pode: {plugin.permissions.map((p) => PERMISSION_LABELS[p] ?? p).join(', ')}.</p>
                {plugin.approval_required.length ? (
                  <p className="text-xs text-text-muted">
                    Pede sua aprovação antes de: {[...new Set(plugin.approval_required.map((a) => ACTION_LABELS[a] ?? a))].join(', ')}.
                  </p>
                ) : null}
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <StatusBadge status={plugin.status} />
                <Toggle label={`Ativar ${PLUGIN_LABELS[plugin.id]?.name ?? plugin.id}`} checked={plugin.enabled}
                  disabled={!reachable} onChange={(next) => onToggle(plugin.id, next)} />
              </div>
            </div>
          </li>
        )
      })}
    </ul>
  )
}

function usePlugins() {
  const { data, error, reload } = useLoad(apiGetIntegrations)
  const [toggleError, setToggleError] = useState<string | null>(null)
  const toggle = useCallback(async (id: string, enabled: boolean) => {
    setToggleError(null)
    try {
      await apiSetPluginEnabled(id, enabled)
      reload()
    } catch (err) {
      setToggleError(err instanceof Error ? err.message : 'Não foi possível salvar.')
    }
  }, [reload])
  return { data, error: error ?? toggleError, toggle }
}

function PluginsSection() {
  const { data, error, toggle } = usePlugins()
  return (
    <>
      <Header title="Plugins" description="Capacidades que o TRINDATRON executa por você em conversas e agentes. Desativar um plugin remove essas ações para você." />
      {data ? <Block title="Plugins do TRINDATRON"><PluginsList integrations={data} onToggle={toggle} /></Block> : <Loading error={error} />}
      {data && error ? <p className="text-xs text-error">{error}</p> : null}
    </>
  )
}

// ── Configurações ────────────────────────────────────────────────────────────

function GeneralSection() {
  const { preferences, error, saving, save } = usePreferences()
  if (!preferences) return <><Header title="Geral" description="Como o TRINDATRON responde para você." /><Loading error={error} /></>
  return (
    <>
      <Header title="Geral" description="Como o TRINDATRON responde para você, em qualquer dispositivo." />
      <Block title="Idioma das respostas">
        <select
          className="w-full bg-surface-2 border border-border rounded-lg px-3 py-2 text-sm text-text-primary"
          value={preferences.language}
          disabled={saving}
          onChange={(event) => void save({ language: event.target.value as Preferences['language'] })}
        >
          <option value="auto">O mesmo idioma em que eu escrever</option>
          <option value="pt">Português</option>
          <option value="en">English</option>
          <option value="es">Español</option>
        </select>
      </Block>
      <Block title="Estilo das respostas">
        <div className="grid grid-cols-3 gap-2">
          {([['concise', 'Direto'], ['balanced', 'Equilibrado'], ['detailed', 'Detalhado']] as const).map(([value, label]) => (
            <button key={value} type="button" disabled={saving} onClick={() => void save({ response_style: value })}
              className={['px-3 py-2 rounded-lg border text-xs transition-colors',
                preferences.response_style === value ? 'border-accent bg-accent/10 text-text-primary' : 'border-border text-text-secondary hover:bg-surface-2'].join(' ')}>
              {label}
            </button>
          ))}
        </div>
        <p className="text-xs text-text-muted mt-3">Vale para conversas, agentes e Computer Use.</p>
      </Block>
      <Block title="Enviar com Enter">
        <div className="flex items-center justify-between gap-3">
          <p className="text-xs text-text-muted">
            {preferences.send_on_enter
              ? 'Enter envia a mensagem; Shift+Enter quebra a linha.'
              : 'Enter quebra a linha; Ctrl+Enter (ou Cmd+Enter) envia.'}
          </p>
          <Toggle label="Enviar com Enter" checked={preferences.send_on_enter} disabled={saving}
            onChange={(next) => void save({ send_on_enter: next })} />
        </div>
      </Block>
    </>
  )
}

const CLIENT_LABELS: Record<string, string> = { web: 'Navegador', mobile: 'Celular', desktop: 'Desktop' }

function AccountSection() {
  const router = useRouter()
  const { user, clearAuth } = useAppStore()
  const sessions = useLoad(apiListDeviceSessions)
  const [busy, setBusy] = useState(false)

  async function signOutAll() {
    if (!window.confirm('Sair de todos os dispositivos, incluindo este?')) return
    setBusy(true)
    try {
      await apiSignOutEverywhere()
      clearAuth()
      router.replace('/auth')
    } finally {
      setBusy(false)
    }
  }

  async function revoke(item: DeviceSession) {
    await apiRevokeDeviceSession(item.id)
    sessions.reload()
  }

  return (
    <>
      <Header title="Conta" description="Sua identidade, seu plano e onde você está conectado." />
      <Block title="Perfil">
        <dl className="text-sm space-y-1.5">
          <div className="flex justify-between"><dt className="text-text-muted">Email</dt><dd className="text-text-primary">{user?.email ?? '—'}</dd></div>
          <div className="flex justify-between"><dt className="text-text-muted">Plano</dt><dd className="text-text-primary uppercase">{user?.plan ?? '—'}</dd></div>
        </dl>
      </Block>
      <SecurityBlocks />
      <Block title="Dispositivos conectados" footer={
        <Button variant="destructive" size="sm" loading={busy} onClick={() => void signOutAll()}>Sair de todos os dispositivos</Button>
      }>
        {sessions.data ? (
          sessions.data.length ? (
            <ul className="divide-y divide-border">
              {sessions.data.map((item) => (
                <li key={item.id} className="flex items-center justify-between py-2.5 gap-3">
                  <div className="min-w-0">
                    <p className="text-sm text-text-primary">{CLIENT_LABELS[item.client] ?? item.client}{item.current ? ' · este dispositivo' : ''}</p>
                    <p className="text-xs text-text-muted truncate">{item.user_agent ?? 'Dispositivo'} · usado em {formatDate(item.last_used_at)}</p>
                  </div>
                  {!item.current ? <Button variant="ghost" size="sm" onClick={() => void revoke(item)}>Desconectar</Button> : null}
                </li>
              ))}
            </ul>
          ) : <p className="text-xs text-text-muted">Nenhum outro dispositivo conectado.</p>
        ) : <Loading error={sessions.error} />}
      </Block>
    </>
  )
}

const SECURITY_INPUT = 'w-full bg-surface-2 border border-border rounded-lg px-3 py-2 text-sm text-text-primary focus:outline-none focus:border-accent/60'

/** Email confirmation, password change and the account's security history. */
function SecurityBlocks() {
  const router = useRouter()
  const { clearAuth } = useAppStore()
  const me = useLoad(apiGetMe)
  const events = useLoad(apiListSecurityEvents)
  const [resent, setResent] = useState<string | null>(null)
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [saving, setSaving] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)

  async function resend() {
    try {
      const already = await apiResendVerification()
      setResent(already ? 'Seu e-mail já está confirmado.' : 'Enviamos um novo link. Confira sua caixa de entrada.')
      if (already) me.reload()
    } catch (err) {
      setResent(err instanceof Error ? err.message : 'Não foi possível enviar agora.')
    }
  }

  async function changePassword(e: FormEvent) {
    e.preventDefault()
    setProblem(null)
    setSaving(true)
    try {
      await apiChangePassword(current, next)
      clearAuth()
      router.replace('/auth')
    } catch (err) {
      setProblem(err instanceof Error ? err.message : 'Não foi possível alterar a senha agora.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <Block title="E-mail">
        {me.data ? (
          me.data.email_verified ? (
            <p className="text-sm text-text-primary">Confirmado</p>
          ) : (
            <div className="space-y-2">
              <p className="text-sm text-text-primary">Ainda não confirmado. Confirmar protege a recuperação da sua conta.</p>
              <Button variant="secondary" size="sm" onClick={() => void resend()}>Enviar link de confirmação</Button>
              {resent ? <p className="text-xs text-text-muted" role="status">{resent}</p> : null}
            </div>
          )
        ) : <Loading error={me.error} />}
      </Block>
      <Block title="Senha">
        <form onSubmit={(e) => void changePassword(e)} className="space-y-3">
          <div className="space-y-1">
            <label htmlFor="current-password" className="block text-xs text-text-muted">Senha atual</label>
            <input id="current-password" type="password" autoComplete="current-password" required value={current}
              onChange={(e) => setCurrent(e.target.value)} className={SECURITY_INPUT} />
          </div>
          <div className="space-y-1">
            <label htmlFor="new-password" className="block text-xs text-text-muted">Nova senha (mínimo 8 caracteres)</label>
            <input id="new-password" type="password" autoComplete="new-password" required minLength={8} value={next}
              onChange={(e) => setNext(e.target.value)} className={SECURITY_INPUT} />
          </div>
          {problem ? <p className="text-xs text-error" role="alert">{problem}</p> : null}
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs text-text-muted">Ao alterar, todos os dispositivos saem da conta.</p>
            <Button type="submit" variant="secondary" size="sm" loading={saving}>Alterar senha</Button>
          </div>
          <p className="text-xs text-text-muted">
            Entra com Google e nunca criou senha? <Link href="/auth/forgot" className="underline hover:text-text-primary">Crie uma pelo e-mail</Link>.
          </p>
        </form>
      </Block>
      <Block title="Atividade de segurança">
        {events.data ? (
          events.data.length ? (
            <ul className="divide-y divide-border">
              {events.data.slice(0, 20).map((event: SecurityEvent, i) => (
                <li key={`${event.created_at}-${i}`} className="py-2">
                  <p className="text-sm text-text-primary">
                    {event.label}{event.method === 'google' ? ' · Google' : ''}
                  </p>
                  <p className="text-xs text-text-muted truncate">
                    {formatDate(event.created_at)}{event.ip ? ` · ${event.ip}` : ''}{event.user_agent ? ` · ${event.user_agent}` : ''}
                  </p>
                </li>
              ))}
            </ul>
          ) : <p className="text-xs text-text-muted">Nenhuma atividade registrada ainda.</p>
        ) : <Loading error={events.error} />}
      </Block>
    </>
  )
}

function PrivacySection() {
  const { preferences, error, saving, save } = usePreferences()
  const [exporting, setExporting] = useState(false)
  const [cleared, setCleared] = useState<string | null>(null)

  async function exportData() {
    setExporting(true)
    try {
      const blob = await apiExportMyData()
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = 'trindatron-export.json'
      document.body.appendChild(link)
      link.click()
      link.remove()
      setTimeout(() => URL.revokeObjectURL(url), 30_000)
    } finally {
      setExporting(false)
    }
  }

  async function clearMemories() {
    if (!window.confirm('Apagar todas as memórias salvas? Isso não pode ser desfeito.')) return
    const { deleted } = await apiClearMemories()
    setCleared(deleted === 1 ? '1 memória apagada.' : `${deleted} memórias apagadas.`)
  }

  return (
    <>
      <Header title="Privacidade" description="O que o TRINDATRON guarda sobre você, e o controle sobre isso." />
      <Block title="Como seus dados são usados">
        <ul className="text-xs text-text-secondary space-y-2 list-disc pl-4">
          <li><strong className="text-text-primary">Contexto da conversa</strong>: as mensagens de uma conversa servem para responder dentro dela.</li>
          <li><strong className="text-text-primary">Memória</strong>: fatos que você salvou ou pediu para lembrar, usados em conversas futuras quando fazem sentido. Você controla abaixo.</li>
          <li><strong className="text-text-primary">Melhoria do produto</strong>: apenas contagens agregadas de quais capacidades são pedidas — nunca o conteúdo das suas mensagens, nem quem pediu.</li>
        </ul>
      </Block>
      <Block title="Usar memória nas respostas">
        {preferences ? (
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs text-text-muted">Desligada, nenhuma memória entra nas respostas. As memórias salvas continuam guardadas até você apagá-las.</p>
            <Toggle label="Usar memória" checked={preferences.memory_enabled} disabled={saving} onChange={(next) => void save({ memory_enabled: next })} />
          </div>
        ) : <Loading error={error} />}
      </Block>
      <Block title="Seus dados">
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" size="sm" loading={exporting} onClick={() => void exportData()}>Baixar meus dados</Button>
          <Button variant="destructive" size="sm" onClick={() => void clearMemories()}>Apagar todas as memórias</Button>
        </div>
        {cleared ? <p className="text-xs text-text-muted mt-2">{cleared}</p> : null}
        <p className="text-xs text-text-muted mt-3">
          O arquivo inclui perfil, preferências, memórias, habilidades, agentes, projetos, conversas, criações e agendados.
          Conversas podem ser apagadas uma a uma. A exclusão completa da conta ainda não está disponível pelo aplicativo.
        </p>
      </Block>
    </>
  )
}

const SUBSCRIPTION_STATUS: Record<string, string> = {
  active: 'Ativa', pending: 'Pendente', past_due: 'Pagamento atrasado', cancelled: 'Cancelada', canceled: 'Cancelada', expired: 'Expirada',
}

function BillingSection() {
  const { user } = useAppStore()
  const subscriptions = useLoad(apiListMySubscriptions)
  const orders = useLoad(() => apiListOrders({ limit: 10 }))
  const [cancelError, setCancelError] = useState<string | null>(null)

  async function cancel(subscription: MySubscription) {
    if (!window.confirm('Cancelar a assinatura? Você mantém o plano até o fim do período já pago.')) return
    setCancelError(null)
    try {
      await apiCancelSubscription(subscription.id)
      subscriptions.reload()
    } catch (err) {
      setCancelError(err instanceof Error ? err.message : 'Não foi possível cancelar.')
    }
  }

  const orderList: PaymentOrder[] = orders.data?.orders ?? []
  return (
    <>
      <Header title="Cobrança" description="Seu plano, assinatura e pagamentos." />
      <Block title="Plano atual" footer={<Link href="/plans" className="text-xs text-accent-light hover:underline">Ver e mudar de plano</Link>}>
        <p className="text-sm text-text-primary uppercase">{user?.plan ?? '—'}</p>
      </Block>
      <Block title="Assinatura">
        {subscriptions.data ? (
          subscriptions.data.length ? (
            <ul className="divide-y divide-border">
              {subscriptions.data.map((subscription) => (
                <li key={subscription.id} className="py-2.5 flex items-center justify-between gap-3">
                  <div>
                    <p className="text-sm text-text-primary uppercase">{subscription.plan} · {subscription.currency} {subscription.amount}</p>
                    <p className="text-xs text-text-muted">
                      {SUBSCRIPTION_STATUS[subscription.status] ?? subscription.status}
                      {subscription.next_billing_at && !subscription.cancelled_at ? ` · renova em ${formatDate(subscription.next_billing_at)}` : ''}
                      {subscription.cancelled_at ? ` · cancelada em ${formatDate(subscription.cancelled_at)}` : ''}
                    </p>
                  </div>
                  {!subscription.cancelled_at && subscription.status === 'active' ? (
                    <Button variant="ghost" size="sm" onClick={() => void cancel(subscription)}>Cancelar assinatura</Button>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : <p className="text-xs text-text-muted">Você não tem assinatura recorrente.</p>
        ) : <Loading error={subscriptions.error} />}
        {cancelError ? <p className="text-xs text-error mt-2">{cancelError}</p> : null}
      </Block>
      <Block title="Pagamentos">
        {orders.data ? (
          orderList.length ? (
            <ul className="divide-y divide-border">
              {orderList.map((order) => (
                <li key={order.id} className="py-2 flex justify-between text-xs">
                  <span className="text-text-secondary">{formatDate(order.created_at)}</span>
                  <span className="text-text-primary">{order.currency_fiat} {order.amount_fiat}</span>
                  <span className="text-text-muted">{order.status}</span>
                </li>
              ))}
            </ul>
          ) : <p className="text-xs text-text-muted">Nenhum pagamento ainda.</p>
        ) : <Loading error={orders.error} />}
      </Block>
    </>
  )
}

function Meter({ label, pct, resetsAt }: { label: string; pct: number | null; resetsAt: string | null }) {
  const value = Math.max(0, Math.min(100, pct ?? 0))
  return (
    <div className="mb-4">
      <div className="flex justify-between text-xs mb-1"><span className="text-text-secondary">{label}</span><span className="text-text-muted">{pct === null ? '—' : `${Math.round(value)}% usado`}</span></div>
      <div className="h-1.5 rounded-full bg-surface-3 overflow-hidden"><div className="h-full bg-accent" style={{ width: `${value}%` }} /></div>
      {resetsAt ? <p className="text-[11px] text-text-muted mt-1">Renova em {formatDate(resetsAt)}</p> : null}
    </div>
  )
}

function UsageSection() {
  const capacity = useLoad<Capacity>(apiGetCapacity)
  const matrix = useLoad(apiGetCapabilityMatrix)
  return (
    <>
      <Header title="Uso" description="Quanto do seu plano você já usou nas janelas atuais." />
      <Block title="Janelas de uso">
        {capacity.data ? (
          <>
            <Meter label="Sessão atual" pct={capacity.data.session.active ? capacity.data.session.pct_used : 0} resetsAt={capacity.data.session.resets_at} />
            <Meter label="Semana" pct={capacity.data.weekly.pct_used} resetsAt={capacity.data.weekly.resets_at} />
            <p className="text-xs text-text-muted">Agentes ativos agora: {capacity.data.active_agents}</p>
          </>
        ) : <Loading error={capacity.error} />}
      </Block>
      <Block title="Criações e leituras de hoje">
        {matrix.data ? <MatrixList matrix={matrix.data} keys={['image_generation', 'video_generation', 'vision']} /> : <Loading error={matrix.error} />}
      </Block>
    </>
  )
}

function CapabilitiesSection() {
  const { data, error } = useLoad(apiGetCapabilityMatrix)
  return (
    <>
      <Header title="Capacidades" description="Tudo o que o TRINDATRON consegue fazer na sua conta agora — exatamente o que ele usa ao responder." />
      {data ? <Block title="Na sua conta"><MatrixList matrix={data} keys={Object.keys(CAPABILITY_LABELS)} /></Block> : <Loading error={error} />}
    </>
  )
}

function MemorySection() {
  const { preferences, error, saving, save } = usePreferences()
  const memories = useLoad(apiListMemories)
  return (
    <>
      <Header title="Memória" description="Fatos sobre você que o TRINDATRON pode usar em conversas futuras." />
      <Block title="Memória nas respostas" footer={<Link href="/memory" className="text-xs text-accent-light hover:underline">Ver e gerenciar memórias</Link>}>
        {preferences ? (
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs text-text-muted">
              {memories.data ? `${memories.data.length} memória(s) salva(s).` : 'Carregando memórias…'} Só as relevantes para o pedido entram na resposta.
            </p>
            <Toggle label="Usar memória" checked={preferences.memory_enabled} disabled={saving} onChange={(next) => void save({ memory_enabled: next })} />
          </div>
        ) : <Loading error={error} />}
      </Block>
      <Block title="O que não é memória">
        <p className="text-xs text-text-muted">O contexto de uma conversa fica nela. Memórias só existem quando são salvas — nada é guardado em segredo.</p>
      </Block>
    </>
  )
}

function ComputerUseSection() {
  const { data, error, toggle } = usePlugins()
  return (
    <>
      <Header title="Computer Use" description="Deixe o TRINDATRON usar o navegador e um computador virtual por você." />
      <Block title="Como funciona" footer={<Link href="/computer-use" className="text-xs text-accent-light hover:underline">Abrir Computer Use</Link>}>
        <p className="text-xs text-text-secondary">
          Você diz o que quer; o TRINDATRON abre o site, encontra a opção, lê, clica e preenche. Ele trabalha num navegador e num
          computador virtual isolados — nunca no seu próprio dispositivo — e para e pede sua aprovação antes de qualquer ação que altere algo.
        </p>
      </Block>
      {data ? <Block title="Permissões"><PluginsList integrations={data} onToggle={toggle} only={['browser', 'computer']} /></Block> : <Loading error={error} />}
    </>
  )
}

function ScheduledSection() {
  return (
    <>
      <Header title="Agendados" description="Instruções que o TRINDATRON cumpre sozinho, no horário que você escolher." />
      <ScheduledTasks />
    </>
  )
}

const JOB_STATUS: Record<string, string> = {
  created: 'Criado', queued: 'Na fila', starting: 'Iniciando', running: 'Executando', waiting: 'Aguardando aprovação',
  completed: 'Concluído', failed: 'Falhou', cancelled: 'Cancelado', expired: 'Expirado', cleanup: 'Finalizando', destroyed: 'Encerrado',
}
const JOB_KIND: Record<string, string> = { research: 'Pesquisa e escrita', shell: 'Execução', presentation: 'Apresentação', agent_decide: 'Decisão de agente' }

function WorkSection() {
  const { data, error } = useLoad(() => apiListExecutionJobs())
  const jobs: ExecutionJob[] = data?.jobs ?? []
  return (
    <>
      <Header title="Trabalho" description="Tarefas longas que o TRINDATRON executa em segundo plano, com o estado real de cada uma." />
      <Block title="Execuções recentes" footer={<div className="flex gap-3"><Link href="/agents" className="text-xs text-accent-light hover:underline">Agentes</Link><Link href="/agent-instances" className="text-xs text-accent-light hover:underline">Agentes persistentes</Link></div>}>
        {data ? (
          jobs.length ? (
            <ul className="divide-y divide-border">
              {jobs.slice(0, 20).map((job) => (
                <li key={job.id} className="py-2 flex items-center justify-between gap-3 text-xs">
                  <span className="text-text-primary">{JOB_KIND[job.job_type] ?? 'Tarefa'}</span>
                  <span className="text-text-muted">{formatDate(job.created_at)}</span>
                  <Badge variant={job.status === 'completed' ? 'success' : job.status === 'failed' ? 'error' : 'default'}>{JOB_STATUS[job.status] ?? job.status}</Badge>
                </li>
              ))}
            </ul>
          ) : <p className="text-xs text-text-muted">Nenhuma execução ainda.</p>
        ) : <Loading error={error} />}
      </Block>
    </>
  )
}

function VoiceSection() {
  const { data, error } = useLoad(apiGetCapabilityMatrix)
  const { preferences, saving, save } = usePreferences()
  const canSpeak = data?.audio_output?.available === true
  return (
    <>
      <Header title="Voz" description="Fale com o TRINDATRON e ouça as respostas." />
      <Block title="Na sua conta">
        {data ? <MatrixList matrix={data} keys={['audio_input', 'transcription', 'audio_output', 'voice_conversation']} /> : <Loading error={error} />}
        <p className="text-xs text-text-muted mt-3">Use o microfone no campo de mensagem. A conversa por voz é por turnos: você fala, o TRINDATRON responde por escrito e pode ler a resposta em voz alta.</p>
      </Block>
      {data && preferences ? (
        <Block title="Ler respostas em voz alta">
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs text-text-muted">
              {canSpeak
                ? 'Cada resposta concluída é lida automaticamente, e cada leitura conta no uso do seu plano.'
                : 'Ouvir respostas não está disponível na sua conta agora.'}
            </p>
            <Toggle label="Ler respostas em voz alta" checked={canSpeak && preferences.voice_auto_play}
              disabled={saving || !canSpeak} onChange={(next) => void save({ voice_auto_play: next })} />
          </div>
        </Block>
      ) : null}
    </>
  )
}

function MediaSection() {
  const { data, error } = useLoad(apiGetCapabilityMatrix)
  return (
    <>
      <Header title="Mídia" description="Imagens e vídeos criados a partir do que você descreve, no formato que você pedir." />
      <Block title="Na sua conta">
        {data ? <MatrixList matrix={data} keys={['image_generation', 'image_editing', 'video_generation', 'image_to_video', 'vision']} /> : <Loading error={error} />}
        <p className="text-xs text-text-muted mt-3">Peça na conversa: “Crie uma imagem vertical 9:16 de um farol” ou “Anime esta imagem por 4 segundos”.</p>
      </Block>
    </>
  )
}

// ── Aplicativo ───────────────────────────────────────────────────────────────

function DesktopSection() {
  return (
    <>
      <Header title="Desktop" description="O TRINDATRON no seu computador." />
      <Block title="Aplicativo desktop">
        <p className="text-xs text-text-secondary">
          O aplicativo desktop está em desenvolvimento e ainda não é distribuído. Ele usará a mesma conta, as mesmas conversas e as mesmas
          configurações desta página — tudo vem do mesmo TRINDATRON.
        </p>
      </Block>
    </>
  )
}

function ExtensionsSection() {
  return (
    <>
      <Header title="Extensões" description="Complementos que ampliam o aplicativo, como integrações com o navegador." />
      <Block title="Disponíveis">
        <p className="text-xs text-text-muted">Nenhuma extensão está disponível ainda.</p>
      </Block>
    </>
  )
}

export const SECTION_COMPONENTS: Record<string, ComponentType> = {
  skills: SkillsSection,
  connectors: ConnectorsSection,
  plugins: PluginsSection,
  general: GeneralSection,
  account: AccountSection,
  privacy: PrivacySection,
  billing: BillingSection,
  usage: UsageSection,
  capabilities: CapabilitiesSection,
  memory: MemorySection,
  'computer-use': ComputerUseSection,
  scheduled: ScheduledSection,
  work: WorkSection,
  voice: VoiceSection,
  media: MediaSection,
  desktop: DesktopSection,
  extensions: ExtensionsSection,
}
