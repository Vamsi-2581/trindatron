import { act, fireEvent, screen, waitFor } from '@testing-library/react-native'
import { renderRouter } from 'expo-router/testing-library'
import { Alert, Dimensions, Text } from 'react-native'
import { KeyboardController } from 'react-native-keyboard-controller'

import { baselineCapabilities } from '@/features/capabilities/capabilities'
import { useCapabilityStore } from '@/features/capabilities/store'
import * as pickers from '@/features/files/pickers'
import { useLocaleStore } from '@/i18n'
import * as attachmentsApi from '@/services/api/attachments'
import * as sessionsApi from '@/services/api/sessions'
import type { Session } from '@/services/api/types'
import * as usageApi from '@/services/api/usage'
import { useAuthStore } from '@/stores/auth'
import { usePendingMessageStore } from '@/stores/pendingMessage'

import AppLayout from '../../app/(app)/_layout'
import MainLayout from '../../app/(app)/(tabs)/_layout'
import HomeScreen from '../../app/(app)/(tabs)/index'

jest.mock('@/services/api/sessions')
jest.mock('@/services/api/usage')
jest.mock('@/services/api/attachments')
jest.mock('@/features/files/pickers', () => ({ ...jest.requireActual('@/features/files/pickers'), pickImageFromLibrary: jest.fn() }))
jest.mock('@/features/voice/useVoiceInput', () => ({
  useVoiceInput: () => ({ phase: 'idle', error: null, durationMs: 0, start: jest.fn(), stop: jest.fn(), cancel: jest.fn(), clearError: jest.fn() }),
}))

const sessions = sessionsApi as jest.Mocked<typeof sessionsApi>
const usage = usageApi as jest.Mocked<typeof usageApi>
const attachments = attachmentsApi as jest.Mocked<typeof attachmentsApi>
const pickImage = pickers.pickImageFromLibrary as jest.MockedFunction<typeof pickers.pickImageFromLibrary>

const PROVIDER_NAMES = /\b(gpt|openai|anthropic|claude|gemini|mistral|grok|xai|provider|fallback)\b/i

const session = (id: string, title: string): Session => ({
  id,
  title,
  project_id: null,
  agent_definition_id: null,
  is_temporary: false,
  created_at: '2026-09-14T10:00:00',
  updated_at: '2026-09-14T10:00:00',
})

const routes = {
  '(app)/_layout': AppLayout,
  '(app)/(tabs)/_layout': MainLayout,
  '(app)/(tabs)/index': HomeScreen,
  '(app)/(tabs)/projects': () => <Text>projects-screen</Text>,
  '(app)/(tabs)/files': () => <Text>files-screen</Text>,
  '(app)/(tabs)/agents': () => <Text>agents-screen</Text>,
  '(app)/(tabs)/profile': () => <Text>profile-screen</Text>,
  '(app)/chat/[sessionId]': () => <Text>chat-screen</Text>,
  '(app)/creations': () => <Text>creations-screen</Text>,
  '(app)/support/index': () => <Text>support-screen</Text>,
  '(app)/plans': () => <Text>plans-screen</Text>,
  '(app)/billing': () => <Text>billing-screen</Text>,
  '(app)/usage': () => <Text>usage-screen</Text>,
}

function setWidth(width: number) {
  Dimensions.set({ window: { width, height: 900, scale: 2, fontScale: 1 }, screen: { width, height: 900, scale: 2, fontScale: 1 } })
}

beforeEach(() => {
  jest.resetAllMocks()
  setWidth(390)
  useLocaleStore.setState({ preference: 'auto', systemLocale: 'pt', locale: 'pt' })
  useAuthStore.setState({
    status: 'signedIn',
    token: 'tok',
    notice: null,
    user: { id: 'u1', email: 'ana@example.com', full_name: 'Ana Souza', plan: 'free', is_active: true, timezone: null, created_at: '2026-09-13T10:00:00' },
  })
  useCapabilityStore.setState({ capabilities: baselineCapabilities(), loaded: true, userId: 'u1', load: jest.fn(async () => undefined) })
  sessions.listSessions.mockResolvedValue({ sessions: [session('s1', 'Plano de lançamento'), session('s2', 'Resumo da reunião')], total: 2 })
  attachments.listAttachments.mockResolvedValue({ attachments: [] })
})

describe('Home', () => {
  it('is the menu, the brand, a greeting and the composer — nothing competing with the conversation', async () => {
    renderRouter(routes, { initialUrl: '/' })

    expect(await screen.findByText('Olá, Ana.')).toBeTruthy()
    expect(screen.getByLabelText('Abrir menu')).toBeTruthy()
    expect(screen.getByLabelText('Adicionar')).toBeTruthy()
    expect(screen.getByLabelText('Mensagem para o TRINDATRON')).toBeTruthy()
    expect(screen.getByLabelText('Gravar mensagem de voz')).toBeTruthy()
    expect(screen.getByLabelText('Enviar mensagem')).toBeTruthy()

    // No tips, no recent conversations, no plan line, no tab bar.
    expect(screen.queryByText(/^(Você sabia\?|Dica|Experimente|Crie)$/)).toBeNull()
    expect(screen.queryByText('Recentes')).toBeNull()
    expect(screen.queryByText('Ver tudo')).toBeNull()
    expect(screen.queryByText('Plano de lançamento')).toBeNull()
    expect(screen.queryByText(/disponível/)).toBeNull()
    for (const tab of ['Início', 'Projetos', 'Arquivos', 'Agentes', 'Perfil']) expect(screen.queryByText(tab)).toBeNull()
    expect(sessions.listSessions).not.toHaveBeenCalled()
    expect(usage.getCapacity).not.toHaveBeenCalled()

    const visibleText = screen
      .UNSAFE_getAllByType(Text)
      .flatMap((node) => [node.props.children].flat())
      .filter((child): child is string => typeof child === 'string')
      .join(' ')
    expect(visibleText).not.toMatch(PROVIDER_NAMES)
  })

  it('opens the side menu with the conversations: search one and open it', async () => {
    renderRouter(routes, { initialUrl: '/' })
    await screen.findByText('Olá, Ana.')

    fireEvent.press(screen.getByLabelText('Abrir menu'))
    expect(await screen.findByText('Conversas')).toBeTruthy()
    fireEvent.changeText(screen.getByLabelText('Buscar conversas'), 'reuniao')
    await waitFor(() => expect(screen.queryAllByLabelText('Plano de lançamento')).toHaveLength(0))

    fireEvent.press(screen.getByLabelText('Resumo da reunião'))
    expect(await screen.findByText('chat-screen')).toBeTruthy()
    expect(screen).toHavePathname('/chat/s2')
  })

  it('closes the search keyboard with the menu, so it never stays over Home', async () => {
    renderRouter(routes, { initialUrl: '/' })
    await screen.findByText('Olá, Ana.')
    const dismiss = KeyboardController.dismiss as jest.Mock
    const visible = KeyboardController.isVisible as jest.Mock

    fireEvent.press(screen.getByLabelText('Abrir menu'))
    fireEvent.changeText(await screen.findByLabelText('Buscar conversas'), 'reuniao')
    dismiss.mockClear()
    visible.mockReturnValueOnce(true)
    fireEvent.press(screen.getAllByLabelText('Fechar menu').at(-1)!)
    expect(dismiss).toHaveBeenCalledTimes(1)
  })

  it('reaches every other place from the side menu', async () => {
    renderRouter(routes, { initialUrl: '/' })
    await screen.findByText('Olá, Ana.')

    fireEvent.press(screen.getByLabelText('Abrir menu'))
    for (const place of ['Projetos', 'Arquivos', 'Agentes', 'Criações', 'Perfil', 'Suporte']) {
      expect(await screen.findByLabelText(place)).toBeTruthy()
    }
    fireEvent.press(screen.getByLabelText('Projetos'))
    expect(await screen.findByText('projects-screen')).toBeTruthy()
    expect(screen).toHavePathname('/projects')
  })

  it.each([
    ['Planos', '/plans', 'plans-screen'],
    ['Cobrança', '/billing', 'billing-screen'],
    ['Uso do plano', '/usage', 'usage-screen'],
    ['Suporte', '/support', 'support-screen'],
  ])('opens %s on its own screen from the side menu, never the Help Center by default', async (label, path, marker) => {
    renderRouter(routes, { initialUrl: '/' })
    await screen.findByText('Olá, Ana.')

    fireEvent.press(screen.getByLabelText('Abrir menu'))
    fireEvent.press(await screen.findByLabelText(label))
    expect(await screen.findByText(marker)).toBeTruthy()
    expect(screen).toHavePathname(path)
  })

  it('starts a message from a suggestion, in the same composer, and suggests only what the account can do', async () => {
    renderRouter(routes, { initialUrl: '/' })
    await screen.findByText('Olá, Ana.')
    // The baseline account cannot create images: no suggestion to do it.
    expect(screen.queryByText('Criar uma imagem')).toBeNull()

    fireEvent.press(screen.getByText('Explicar um assunto'))
    expect(screen.getByLabelText('Mensagem para o TRINDATRON').props.value).toBe('Explique de forma simples: ')
    expect(sessions.createSession).not.toHaveBeenCalled()
  })

  it('groups the history by day and deletes a conversation from its menu', async () => {
    const today = new Date().toISOString()
    sessions.listSessions.mockResolvedValue({
      sessions: [{ ...session('s1', 'Plano de lançamento'), updated_at: today }, { ...session('s2', 'Resumo da reunião'), updated_at: '2025-01-10T10:00:00' }],
      total: 2,
    })
    sessions.deleteSession.mockResolvedValue(undefined)
    const alert = jest.spyOn(Alert, 'alert').mockImplementation((_title, _message, buttons) => buttons?.[1]?.onPress?.())
    renderRouter(routes, { initialUrl: '/' })
    await screen.findByText('Olá, Ana.')

    fireEvent.press(screen.getByLabelText('Abrir menu'))
    expect(await screen.findByText('Hoje')).toBeTruthy()
    expect(screen.getByText('Anteriores')).toBeTruthy()
    expect(screen.getByText('Ferramentas')).toBeTruthy()

    fireEvent.press(screen.getByLabelText('Ações da conversa Resumo da reunião'))
    fireEvent.press(screen.getByLabelText('Apagar conversa'))
    await waitFor(() => expect(sessions.deleteSession).toHaveBeenCalledWith('s2'))
    await waitFor(() => expect(screen.queryByLabelText('Resumo da reunião')).toBeNull())
    alert.mockRestore()
  })

  it('on a wide screen keeps the menu docked beside the content, with nothing to open', async () => {
    setWidth(1280)
    renderRouter(routes, { initialUrl: '/' })
    await screen.findByText('Olá, Ana.')
    expect(await screen.findByText('Conversas')).toBeTruthy()
    expect(screen.queryByLabelText('Abrir menu')).toBeNull()
  })

  it('attaches a picked image to a new conversation, and that conversation opens with the message', async () => {
    sessions.createSession.mockResolvedValue(session('s9', 'Nova conversa'))
    pickImage.mockResolvedValue({ kind: 'image', uri: 'file:///foto.jpg', name: 'foto.jpg', mimeType: 'image/jpeg', file: null, previewUri: 'file:///foto.jpg', sizeBytes: 2048 })
    attachments.attachUpload.mockResolvedValue({
      id: 'a1',
      session_id: 's9',
      name: 'foto.jpg',
      mime_type: 'image/jpeg',
      size_bytes: 2048,
      kind: 'image',
      project_file_id: null,
      readable: false,
      created_at: '2026-09-14T10:00:00',
      content_path: '/sessions/s9/attachments/a1/content',
    })
    renderRouter(routes, { initialUrl: '/' })
    await screen.findByText('Olá, Ana.')

    fireEvent.press(screen.getByLabelText('Adicionar'))
    await act(async () => {
      fireEvent.press(await screen.findByLabelText('Galeria, Anexa a esta conversa'))
    })

    // Attached only once the Core answered with the attachment.
    expect(await screen.findByText('foto.jpg (conteúdo não lido)')).toBeTruthy()
    expect(attachments.attachUpload).toHaveBeenCalledWith('s9', expect.objectContaining({ name: 'foto.jpg' }), expect.anything())

    fireEvent.changeText(screen.getByLabelText('Mensagem para o TRINDATRON'), 'O que acha desta foto?')
    await act(async () => {
      fireEvent.press(screen.getByLabelText('Enviar mensagem'))
    })

    expect(await screen.findByText('chat-screen')).toBeTruthy()
    expect(screen).toHavePathname('/chat/s9')
    expect(sessions.createSession).toHaveBeenCalledTimes(1)
    expect(usePendingMessageStore.getState().take('s9')).toBe('O que acha desta foto?')
  })

  it('closes the keyboard on Home when a message is sent, before the conversation opens', async () => {
    // Carried into the conversation, the keyboard was closed there and Home, left behind, stayed raised for it.
    sessions.createSession.mockResolvedValue(session('s7', 'Nova conversa'))
    const dismiss = KeyboardController.dismiss as jest.Mock
    dismiss.mockClear()
    renderRouter(routes, { initialUrl: '/' })
    await screen.findByText('Olá, Ana.')

    fireEvent.changeText(screen.getByLabelText('Mensagem para o TRINDATRON'), 'Oi')
    await act(async () => {
      fireEvent.press(screen.getByLabelText('Enviar mensagem'))
    })
    expect(dismiss).toHaveBeenCalled()
    const dismissedFirst = dismiss.mock.invocationCallOrder[0]!
    expect(dismissedFirst).toBeLessThan((sessions.createSession as jest.Mock).mock.invocationCallOrder[0]!)
    expect(await screen.findByText('chat-screen')).toBeTruthy()
  })

  it('cancelling the picker attaches nothing and opens no conversation', async () => {
    pickImage.mockResolvedValue(null)
    renderRouter(routes, { initialUrl: '/' })
    await screen.findByText('Olá, Ana.')

    fireEvent.press(screen.getByLabelText('Adicionar'))
    await act(async () => {
      fireEvent.press(await screen.findByLabelText('Galeria, Anexa a esta conversa'))
    })

    expect(sessions.createSession).not.toHaveBeenCalled()
    expect(attachments.attachUpload).not.toHaveBeenCalled()
  })
})
