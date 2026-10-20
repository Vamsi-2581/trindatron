/**
 * Proof that @kesha-antonov/react-native-chat works under our contracts — written before any migration,
 * because adopting a library for the product's main surface on the strength of its README is not a proof.
 *
 * What is asserted here is exactly what the migration depends on: the list is *controlled* (it renders the
 * messages we hand it and holds no state of its own), sending goes through our callback, and we can inject
 * our own views into a bubble — which is how media cards, the approval gate and agent steps survive.
 */
import { act, fireEvent, render, screen } from '@testing-library/react-native'
import type { ReactElement } from 'react'
import { Text } from 'react-native'
import { State } from 'react-native-gesture-handler'
import { fireGestureHandler, getByGestureTestId } from 'react-native-gesture-handler/jest-utils'
import { Chat, type IMessage } from '@kesha-antonov/react-native-chat'

const me = { _id: 'me' }
const trindatron = { _id: 'trindatron', name: 'TRINDATRON' }

const unavailable = 'Criação de imagens não está disponível neste momento.'
const answer: IMessage = { _id: 'answer', text: unavailable, createdAt: new Date(0), user: trindatron }
const question: IMessage = { _id: 'question', text: 'Você cria imagens?', createdAt: new Date(0), user: me }
// Newest first: the list is inverted, as ours is.
const messages: IMessage[] = [answer, question]

/** Gesture handler schedules a state update on a timer when a ScrollView mounts; let it land. */
const settle = () => act(async () => {})

/**
 * The library shows nothing until the platform reports a real layout: `onLayout` on the wrapper sets
 * `isInitialized`, and until it does, both the message list and the input toolbar render `null`. Jest
 * never lays views out, so the event the native side would have sent is fired here. The handler ignores
 * a zero height, hence a real one.
 */
const renderChat = async (ui: ReactElement) => {
  const result = render(ui)
  fireEvent(screen.getByTestId('GC_WRAPPER'), 'layout', {
    nativeEvent: { layout: { x: 0, y: 0, width: 390, height: 844 } },
  })
  await settle()
  return result
}

describe('the chat library, under our contracts', () => {
  it('renders the conversation it is given', async () => {
    await renderChat(<Chat messages={messages} user={me} onSend={jest.fn()} enableKeyboardProvider={false} />)

    expect(screen.getByText('Você cria imagens?')).toBeTruthy()
    expect(screen.getByText(unavailable)).toBeTruthy()
  })

  it('is controlled: a new list of messages replaces what is on screen', async () => {
    const { rerender } = await renderChat(
      <Chat messages={messages} user={me} onSend={jest.fn()} enableKeyboardProvider={false} />,
    )
    const streamed: IMessage[] = [{ ...answer, text: `${unavailable} Posso descrever uma.` }, question]

    rerender(<Chat messages={streamed} user={me} onSend={jest.fn()} enableKeyboardProvider={false} />)
    await settle()

    // Streaming rewrites the text of a message in place, which is how our reducer already works.
    expect(screen.getByText(`${unavailable} Posso descrever uma.`)).toBeTruthy()
    expect(screen.queryByText(unavailable)).toBeNull()
  })

  it('sends through our callback and shows nothing until we hand the message back', async () => {
    const onSend = jest.fn()
    await renderChat(
      <Chat
        messages={messages}
        user={me}
        onSend={onSend}
        enableKeyboardProvider={false}
        textInputProps={{ placeholder: 'Mensagem' }}
      />,
    )

    // The composer's testID is its placeholder.
    fireEvent.changeText(screen.getByTestId('Mensagem'), '  Crie um logo  ')
    /*
     * The send button is a gesture-handler 3 BaseButton: it presses through a native gesture, not an
     * `onPress` prop, so fireEvent.press never reaches it. The tap is driven through that gesture instead.
     */
    await act(async () => {
      fireGestureHandler(getByGestureTestId('GC_SEND_TOUCHABLE'), [
        { state: State.BEGAN, pointerInside: true },
        { state: State.ACTIVE, pointerInside: true },
        { state: State.END, pointerInside: true },
      ])
    })

    expect(onSend).toHaveBeenCalledTimes(1)
    expect(onSend).toHaveBeenCalledWith([expect.objectContaining({ text: 'Crie um logo', user: me })])
    // Our reducer owns the conversation: the library must not add the message on its own.
    expect(screen.queryByText('Crie um logo')).toBeNull()
    expect(screen.getByTestId('Mensagem').props.value).toBe('')
  })

  it('lets us render our own view inside a bubble, for media, approval and agent steps', async () => {
    await renderChat(
      <Chat
        messages={messages}
        user={me}
        onSend={jest.fn()}
        enableKeyboardProvider={false}
        renderCustomView={({ currentMessage }: { currentMessage?: IMessage }) => (
          <Text>ours:{String(currentMessage?._id)}</Text>
        )}
      />,
    )

    expect(screen.getByText('ours:answer')).toBeTruthy()
    expect(screen.getByText('ours:question')).toBeTruthy()
  })
})
