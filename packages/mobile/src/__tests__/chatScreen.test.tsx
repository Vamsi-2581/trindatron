import { act, fireEvent, screen } from '@testing-library/react-native'
import { renderRouter } from 'expo-router/testing-library'
import { Alert, StyleSheet } from 'react-native'
import { KeyboardController } from 'react-native-keyboard-controller'

import { baselineCapabilities } from '@/features/capabilities/capabilities'
import { useCapabilityStore } from '@/features/capabilities/store'
import { initialChatState, type ChatMessage, type ChatState } from '@/features/chat/chatReducer'
import * as chatSession from '@/features/chat/useChatSession'
import { pendingCreation } from '@/features/media/media'
import pt from '@/i18n/locales/pt.json'
import * as attachmentsApi from '@/services/api/attachments'
import * as sessionsApi from '@/services/api/sessions'

import ChatScreen from '../../app/(app)/chat/[sessionId]'

jest.mock('@/features/voice/useVoiceInput', () => ({
  useVoiceInput: () => ({ phase: 'idle', error: null, durationMs: 0, start: jest.fn(), stop: jest.fn(), clearError: jest.fn() }),
}))
jest.mock('@/features/chat/useChatSession')
jest.mock('@/services/api/attachments')
jest.mock('@/services/api/sessions')
// A creation card follows its job through the Core; here it shows what it was handed.
jest.mock('@/features/media/useMediaJob', () => ({ useMediaJob: () => ({ job: null }) }))

const useChatSession = chatSession.useChatSession as jest.MockedFunction<typeof chatSession.useChatSession>
const attachments = attachmentsApi as jest.Mocked<typeof attachmentsApi>
const sessions = sessionsApi as jest.Mocked<typeof sessionsApi>

const NOW = new Date().toISOString()

function message(id: string, role: ChatMessage['role'], content: string, overrides: Partial<ChatMessage> = {}): ChatMessage {
  return { id, role, content, createdAt: NOW, state: 'final', ...overrides }
}

function session(overrides: Partial<ChatState> = {}) {
  return {
    state: { ...initialChatState, phase: 'ready' as const, title: 'Plano de lançamento', ...overrides },
    send: jest.fn(async () => undefined),
    cancel: jest.fn(),
    retry: jest.fn(async () => undefined),
    resolveApproval: jest.fn(async () => undefined),
    dismissError: jest.fn(),
    reload: jest.fn(async () => undefined),
  }
}

/** Where the chat starts in the window: under the status bar and the header this screen draws. */
const CHAT_TOP = 57
let measureInWindow: jest.Mock | null = null

/**
 * Opens the conversation the way a device does. The chat shows nothing until the platform reports a real layout and
 * where the chat sits in the window; Jest does neither, so both are supplied here.
 */
async function openChat(overrides: Partial<ChatState> = {}) {
  const value = session(overrides)
  useChatSession.mockReturnValue(value)
  renderRouter({ index: () => null, 'chat/[sessionId]': ChatScreen, usage: () => null }, { initialUrl: '/chat/s1' })
  const wrapper = await screen.findByTestId('GC_WRAPPER')
  // Jest's views share one measureInWindow stub; only the chat asks where it sits, so it answers for the chat.
  measureInWindow = screen.UNSAFE_root.findAll((node) => node.props.testID === 'GC_WRAPPER' && Boolean(node.instance?.measureInWindow))[0]!
    .instance.measureInWindow as jest.Mock
  measureInWindow.mockImplementation((callback: (x: number, y: number) => void) => callback(0, CHAT_TOP))
  fireEvent(wrapper, 'layout', { nativeEvent: { layout: { x: 0, y: 0, width: 390, height: 787 } } })
  await act(async () => {})
  return value
}

/** How many times a view is mirrored on its way to the screen: an even count reads upright. */
function mirrorings(node: { type: unknown; props: { style?: unknown }; parent: unknown } | null): number {
  let count = 0
  for (let current = node; current; current = current.parent as typeof node) {
    if (typeof current.type !== 'string') continue
    const transform = (StyleSheet.flatten(current.props.style as object) as { transform?: Record<string, number>[] } | undefined)?.transform
    count += transform?.filter((step) => step.scaleY === -1).length ?? 0
  }
  return count
}

afterEach(() => {
  measureInWindow?.mockReset()
  measureInWindow = null
})

beforeEach(() => {
  attachments.listAttachments.mockResolvedValue({ attachments: [] })
  useCapabilityStore.setState({ capabilities: baselineCapabilities(), loaded: true })
})

describe('chat screen', () => {
  it('offers deleting the conversation from its ⋯ menu, and nothing the Core cannot do', async () => {
    sessions.deleteSession.mockResolvedValue(undefined)
    const alert = jest.spyOn(Alert, 'alert').mockImplementation((_title, _message, buttons) => buttons?.[1]?.onPress?.())
    await openChat()

    fireEvent.press(screen.getByLabelText(pt.chat.menu))
    // Renaming waits for a Core endpoint: the menu does not pretend.
    expect(screen.queryByText(/Renomear/)).toBeNull()
    fireEvent.press(screen.getByLabelText(pt.chat.deleteConversation))

    await act(async () => {})
    expect(alert).toHaveBeenCalled()
    expect(sessions.deleteSession).toHaveBeenCalledWith('s1')
    expect(screen).toHavePathname('/')
    alert.mockRestore()
  })

  it('keeps the composer on the keyboard from where the chat really starts, never from a guessed header height', async () => {
    await openChat()

    expect(screen.getByText('Plano de lançamento')).toBeTruthy()
    expect(screen.getByLabelText(pt.chat.inputA11y)).toBeTruthy()
    // One keyboard layout on the screen — the chat's — and its offset is the measured top edge.
    const avoiding = screen.UNSAFE_root.findAll((node) => typeof node.type === 'string' && 'keyboardVerticalOffset' in node.props)
    expect(avoiding).toHaveLength(1)
    expect(avoiding[0]!.props.keyboardVerticalOffset).toBe(CHAT_TOP)
  })

  it('closes a keyboard it opened under, and leaves the keyboard alone when there is none', async () => {
    // Sent from Home with the keyboard up: on a physical device the new conversation laid out under it.
    const visible = KeyboardController.isVisible as jest.Mock
    const dismiss = KeyboardController.dismiss as jest.Mock
    dismiss.mockClear()
    visible.mockReturnValueOnce(true)
    await openChat()
    expect(dismiss).toHaveBeenCalledTimes(1)

    dismiss.mockClear()
    visible.mockReturnValue(false)
    await openChat()
    expect(dismiss).not.toHaveBeenCalled()
  })

  it('draws every message with our bubbles', async () => {
    await openChat({
      messages: [
        message('u1', 'user', 'Crie um logo'),
        message('a1', 'assistant', 'Aqui está a primeira versão.', { media: [pendingCreation('job-1', NOW)] }),
        message('a2', 'assistant', 'Parte da resposta', { state: 'stopped' }),
      ],
    })

    expect(screen.getByText('Crie um logo')).toBeTruthy()
    expect(screen.getByText('Aqui está a primeira versão.')).toBeTruthy()
    expect(screen.getByLabelText(new RegExp(pt.media.status.queued))).toBeTruthy()
    expect(screen.getByText(pt.chat.stopped)).toBeTruthy()
    // Our actions, not the library's long-press menu.
    expect(screen.getAllByLabelText(pt.chat.copy)).toHaveLength(2)
  })

  it('shows what the reply in progress is doing on that reply alone, with no cancel action', async () => {
    await openChat({
      streaming: true,
      activeTurn: { userId: 'u2', assistantId: 'a2' },
      activity: 'Pesquisando na web',
      agentSteps: [{ type: 'tool_call', tool: 'web_search' }],
      messages: [
        message('u1', 'user', 'Oi'),
        message('a1', 'assistant', 'Olá!'),
        message('u2', 'user', 'Qual a cotação do dólar?'),
        message('a2', 'assistant', '', { state: 'streaming' }),
      ],
    })

    expect(screen.getAllByText('Pesquisando na web')).toHaveLength(1)
    expect(screen.getAllByLabelText(pt.chat.actionsOne)).toHaveLength(1)
    expect(screen.queryByLabelText(/parar resposta|cancelar|stop/i)).toBeNull()
    expect(screen.getByLabelText(pt.chat.replying)).toBeTruthy()
  })

  it('offers to read an answer aloud only when the Core says this account can hear one', async () => {
    const capabilities = baselineCapabilities()
    capabilities.audio_output = { available: true, status: 'available', dailyQuota: null, limits: null }
    useCapabilityStore.setState({ capabilities, loaded: true })
    await openChat({ messages: [message('u1', 'user', 'Resuma'), message('a1', 'assistant', 'Resumo pronto.')] })

    expect(screen.getByLabelText(pt.chat.listen)).toBeTruthy()
  })

  it('anchors the conversation at its newest message, holding older ones in place while a reply grows', async () => {
    await openChat({
      messages: [message('m1', 'user', 'Primeira'), message('m2', 'assistant', 'Resposta'), message('m3', 'user', 'Última')],
    })

    // The list as configured, before FlatList shifts the index past the header cell it adds on its way to the ScrollView.
    const list = screen.UNSAFE_root.findAll((node) => typeof node.type !== 'string' && node.props.testID === 'chat-messages')[0]!
    expect(list.props.inverted).toBe(true)
    expect(list.props.data.map((row: { _id: string }) => row._id)).toEqual(['m3', 'm2', 'm1'])
    expect(list.props.maintainVisibleContentPosition).toEqual({ minIndexForVisible: 1, autoscrollToTopThreshold: 96 })
    expect(screen.queryByLabelText(pt.chat.jumpToLatest)).toBeNull()
  })

  it('offers the latest message back after scrolling away from it', async () => {
    await openChat({ messages: [message('m1', 'user', 'Oi')] })

    act(() => {
      fireEvent.scroll(screen.getByTestId('chat-messages'), {
        nativeEvent: { contentOffset: { x: 0, y: 600 }, contentSize: { width: 390, height: 2400 }, layoutMeasurement: { width: 390, height: 700 } },
      })
    })
    expect(screen.getByLabelText(pt.chat.jumpToLatest)).toBeTruthy()
  })

  it('sends what is typed through the conversation, from our composer', async () => {
    const chat = await openChat({ messages: [message('m1', 'assistant', 'Como posso ajudar?')] })

    fireEvent.changeText(screen.getByLabelText(pt.chat.inputA11y), 'Crie um roteiro')
    fireEvent.press(screen.getByLabelText(pt.chat.send))

    expect(chat.send).toHaveBeenCalledWith('Crie um roteiro')
  })

  it('asks for approval before an agent acts, and passes the answer on', async () => {
    const chat = await openChat({
      streaming: true,
      activeTurn: { userId: 'u1', assistantId: 'a1' },
      approval: { action_id: 'act-1', action: 'write_file', args: {}, description: 'Salvar relatorio.md no projeto' },
      messages: [message('u1', 'user', 'Salve o relatório'), message('a1', 'assistant', '', { state: 'streaming' })],
    })

    expect(screen.getByText(pt.chat.approvalTitle)).toBeTruthy()
    expect(screen.getByText('Salvar relatorio.md no projeto')).toBeTruthy()
    await act(async () => fireEvent.press(screen.getByText(pt.chat.approve)))
    expect(chat.resolveApproval).toHaveBeenCalledWith(true)
  })

  it('explains a failed turn and offers what can actually be done about it', async () => {
    const chat = await openChat({
      messages: [message('u1', 'user', 'Gere um vídeo')],
      turnError: { message: 'Você atingiu o limite de hoje.', code: 'quota_exceeded', content: 'Gere um vídeo', retryable: true },
    })

    expect(screen.getByText('Você atingiu o limite de hoje.')).toBeTruthy()
    expect(screen.getByText(pt.chat.seePlans)).toBeTruthy()
    fireEvent.press(screen.getByText(pt.common.retry))
    expect(chat.retry).toHaveBeenCalled()
    fireEvent.press(screen.getByLabelText(pt.chat.dismiss))
    expect(chat.dismissError).toHaveBeenCalled()
  })

  it('reads the empty, loading and error states upright inside the inverted list', async () => {
    await openChat()
    expect(mirrorings(screen.getByText(pt.chat.empty)) % 2).toBe(0)
    screen.unmount()

    await openChat({ phase: 'loading' })
    expect(mirrorings(screen.getByText(pt.chat.loading)) % 2).toBe(0)
    screen.unmount()

    const chat = await openChat({ phase: 'error', loadError: null })
    expect(mirrorings(screen.getByText(pt.chat.loadFailed)) % 2).toBe(0)
    fireEvent.press(screen.getByText(pt.common.retry))
    expect(chat.reload).toHaveBeenCalled()
  })

  it('marks where each day of the conversation starts, once per day', async () => {
    await openChat({
      messages: [
        message('m1', 'user', 'Começamos ontem?', { createdAt: '2025-12-31T15:00:00' }),
        message('m2', 'user', 'Bom dia'),
        message('m3', 'assistant', 'Bom dia!'),
      ],
    })

    expect(screen.getByText('31 de dezembro de 2025')).toBeTruthy()
    expect(screen.getAllByText(pt.chat.today)).toHaveLength(1)
  })

  it('names an untitled conversation in the user language', async () => {
    await openChat({ title: 'New conversation' })
    expect(screen.getByText('Nova conversa')).toBeTruthy()
  })
})
