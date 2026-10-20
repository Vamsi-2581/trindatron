import { Chat, type DayProps, type MessageProps } from '@kesha-antonov/react-native-chat'
import { router, useLocalSearchParams } from 'expo-router'
import { useCallback, useEffect, useMemo, useRef, useState, type ComponentProps } from 'react'
import { Platform, StyleSheet, View, type FlatList, type LayoutChangeEvent } from 'react-native'
import { KeyboardController } from 'react-native-keyboard-controller'
import type { ScrollEvent } from 'react-native-reanimated'
import { SafeAreaView, useSafeAreaInsets } from 'react-native-safe-area-context'

import { Composer } from '@/components/chat/Composer'
import { DaySeparator } from '@/components/chat/DaySeparator'
import { MessageBubble } from '@/components/chat/MessageBubble'
import { ActionMenu, AppHeader, AppText, BrandSymbol, Button, CONTENT_MAX_WIDTH, ErrorState, IconButton, LoadingState, Notice, OfflineBanner } from '@/components/ui'
import { ContinueAnswerContext } from '@/features/chat/continueAnswer'
import { PERSON, toConversationRows, type ConversationRow } from '@/features/chat/conversationRows'
import { LATEST_THRESHOLD } from '@/features/chat/scroll'
import { uprightStyle } from '@/features/chat/upright'
import { useChatSession } from '@/features/chat/useChatSession'
import { PLANS_HREF } from '@/features/navigation/routes'
import { useIsOffline } from '@/hooks/useNetworkStatus'
import { useLocale, useT } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import { deleteSession } from '@/services/api/sessions'
import { usePendingMessageStore } from '@/stores/pendingMessage'
import { radius, space, useTheme } from '@/theme'
import { confirmAction } from '@/utils/confirm'
import { conversationTitle } from '@/utils/format'

const PLAN_ERROR = /limit|quota|entitled|forbidden/i
/** The list's own flip undone (features/chat/upright.ts); the platform never changes while the app runs. */
const UPRIGHT = uprightStyle()

/**
 * The list the library mounts is gesture-handler's FlatList, whose ref is React Native's FlatList. The library still
 * types that ref for gesture-handler 2, where the component was a class, so the one ref crosses over with a cast.
 */
type LibraryListRef = ComponentProps<typeof Chat<ConversationRow>>['messagesContainerRef']

/** Every row is ours: the library positions it, MessageBubble draws it. */
function renderMessage({ currentMessage }: MessageProps<ConversationRow>) {
  return (
    <View style={styles.row}>
      <MessageBubble
        message={currentMessage.message}
        activity={currentMessage.activity}
        agentSteps={currentMessage.agentSteps}
        latest={currentMessage.latest}
      />
    </View>
  )
}

function renderDay({ createdAt }: DayProps) {
  return <DaySeparator createdAt={createdAt} />
}

/** The library's own typing indicator would sit beside ours; the reply in progress already says what it is doing. */
const renderNothing = () => null

/**
 * The conversation is laid out by @kesha-antonov/react-native-chat and drawn entirely by TRINDATRON: our bubbles,
 * our composer (attachments, voice, creations, capabilities), our banners. The library brings the parts it does
 * better than a hand-rolled FlatList — rows that re-render only when their own data changes, day separators for
 * history, and a keyboard layout that measures where the chat starts instead of relying on a header height.
 *
 * Scrolling: the list is inverted, so its resting position is the newest message. New messages, a reply streaming
 * in and the keyboard opening all happen at that anchored end, so nothing on screen jumps. Reading older messages is
 * left alone (maintainVisibleContentPosition keeps them in place while the reply grows below), and a button brings
 * the latest back.
 *
 * Keyboard layout: the chat measures its own top edge in the window and keeps the composer on the keyboard from
 * there, so the header this screen draws never needs to be accounted for by hand. The composer drops its
 * home-indicator inset while the keyboard is up so it sits right on top of it.
 */
export default function ChatScreen() {
  const { sessionId } = useLocalSearchParams<{ sessionId: string }>()
  const { scheme, colors } = useTheme()
  const t = useT()
  const locale = useLocale()
  const insets = useSafeAreaInsets()
  const offline = useIsOffline()
  const chat = useChatSession(sessionId)

  // A conversation can open under a keyboard it does not own: send from Home with the keyboard up, or pick a
  // conversation while typing in the menu's search. No input here is focused and no keyboard event follows, so the
  // list and the composer lay out as if the keyboard were closed and end up behind it — on a physical Android 16
  // device the new conversation showed only "Hoje". The keyboard belongs to the screen that opened it; close it.
  useEffect(() => {
    if (KeyboardController.isVisible()) void KeyboardController.dismiss()
  }, [sessionId])
  const { state, send, reload, retry, dismissError, resolveApproval: resolve } = chat
  const title = conversationTitle(state.title)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  // DELETE /sessions/{id} is the one conversation action the Core has; renaming waits for its endpoint.
  async function removeConversation() {
    const confirmed = await confirmAction({
      title: t('chats.deleteTitle'),
      message: t('chats.deleteMessage', { title }),
      confirmLabel: t('common.delete'),
      destructive: true,
    })
    if (!confirmed) return
    setDeleteError(null)
    try {
      await deleteSession(sessionId)
      router.replace('/')
    } catch (cause) {
      setDeleteError(userMessage(cause))
    }
  }
  const listRef = useRef<FlatList<ConversationRow>>(null)
  const consumedPending = useRef(false)
  const [approvalBusy, setApprovalBusy] = useState(false)
  const [awayFromLatest, setAwayFromLatest] = useState(false)
  const [bannersHeight, setBannersHeight] = useState(0)

  // Only what a row is built from: banners and errors changing must not rebuild the list.
  const { messages, activeTurn, activity, agentSteps } = state
  const rows = useMemo(
    () => toConversationRows({ messages, activeTurn, activity, agentSteps }),
    [messages, activeTurn, activity, agentSteps],
  )
  const hasMessages = rows.length > 0

  useEffect(() => {
    if (state.phase !== 'ready' || consumedPending.current) return
    consumedPending.current = true
    const pending = usePendingMessageStore.getState().take(sessionId)
    if (pending) void send(pending)
  }, [state.phase, sessionId, send])

  const jumpToLatest = useCallback(() => {
    listRef.current?.scrollToOffset({ offset: 0, animated: true })
  }, [])

  const onSend = useCallback(
    (content: string) => {
      // What the person just sent is what they want to see, wherever they were.
      jumpToLatest()
      void send(content)
    },
    [send, jumpToLatest],
  )

  // An answer cut at the plan's output limit goes on from where it stopped (MessageBubble offers it).
  const continueAnswer = useCallback(() => onSend(t('chat.continuePrompt')), [onSend, t])

  const onScroll = useCallback((event: ScrollEvent) => {
    // Inverted: offset 0 is the newest message.
    const away = event.contentOffset.y > LATEST_THRESHOLD
    setAwayFromLatest((current) => (current === away ? current : away))
  }, [])

  const listProps = useMemo(
    () => ({
      testID: 'chat-messages',
      onScroll,
      scrollEventThrottle: 64,
      maintainVisibleContentPosition: { minIndexForVisible: 1, autoscrollToTopThreshold: LATEST_THRESHOLD },
      keyboardDismissMode: Platform.OS === 'ios' ? ('interactive' as const) : ('on-drag' as const),
      contentContainerStyle: styles.list,
      initialNumToRender: 15,
      windowSize: 11,
    }),
    [onScroll],
  )

  // The library paints its list background from its own theme; ours is the only palette on this screen.
  const chatTheme = useMemo(() => ({ colors: { background: colors.bg } }), [colors.bg])

  const renderChatEmpty = useCallback(
    () => (
      /*
       * The library hands this to an inverted FlatList as a component, and FlatList only turns its *style* upright —
       * which a component ignores — so without this flip the empty, loading and error states read upside down.
       * UPRIGHT matches the list's own transform per platform (features/chat/upright.ts); chatScreen.test.tsx holds
       * the orientation, so a library fix shows up there instead of on screen.
       */
      <View style={[styles.flex, UPRIGHT]}>
        {state.phase === 'loading' ? (
          <View style={styles.center}>
            <LoadingState label={t('chat.loading')} />
          </View>
        ) : state.phase === 'error' ? (
          <View style={styles.center}>
            <ErrorState message={state.loadError ?? t('chat.loadFailed')} onRetry={reload} />
          </View>
        ) : (
          <View style={[styles.center, styles.empty]}>
            <BrandSymbol size={72} />
            <AppText variant="heading" style={styles.centerText}>
              {t('chat.empty')}
            </AppText>
          </View>
        )}
      </View>
    ),
    [state.phase, state.loadError, reload, t],
  )

  const resolveApproval = useCallback(
    async (approved: boolean) => {
      setApprovalBusy(true)
      await resolve(approved)
      setApprovalBusy(false)
    },
    [resolve],
  )

  const onBannersLayout = useCallback((event: LayoutChangeEvent) => {
    const height = Math.round(event.nativeEvent.layout.height)
    setBannersHeight((current) => (current === height ? current : height))
  }, [])

  /*
   * Rendered under the list, inside the part of the chat that moves with the keyboard. The jump button is positioned
   * against that same container, just above the banners, so it floats over the newest messages and stays inside its
   * parent's bounds — where every platform delivers its touches.
   */
  const renderChatFooter = useCallback(
    () => (
      <>
        {awayFromLatest && hasMessages ? (
          <IconButton
            icon="arrow-down"
            label={t('chat.jumpToLatest')}
            onPress={jumpToLatest}
            style={[styles.jump, { bottom: bannersHeight + space.md, backgroundColor: colors.surface2, borderColor: colors.border }]}
          />
        ) : null}

        <View style={styles.banners} onLayout={onBannersLayout}>
          {state.notice ? <Notice tone="warning">{state.notice}</Notice> : null}

          {state.approval ? (
            <View style={[styles.banner, { backgroundColor: colors.warningMuted, borderColor: colors.warning }]} accessibilityRole="alert">
              <AppText variant="bodyStrong">{t('chat.approvalTitle')}</AppText>
              <AppText variant="caption" tone="secondary">
                {state.approval.description}
              </AppText>
              <View style={styles.bannerActions}>
                <Button label={t('chat.reject')} variant="secondary" disabled={approvalBusy} onPress={() => void resolveApproval(false)} />
                <Button label={t('chat.approve')} icon="checkmark" loading={approvalBusy} onPress={() => void resolveApproval(true)} />
              </View>
            </View>
          ) : null}

          {state.turnError ? (
            <View style={[styles.banner, { backgroundColor: colors.errorMuted, borderColor: colors.error }]} accessibilityRole="alert">
              <View style={styles.bannerHeader}>
                <AppText variant="caption" tone="error" style={styles.flex}>
                  {state.turnError.message}
                </AppText>
                <IconButton icon="close" label={t('chat.dismiss')} size="md" onPress={dismissError} />
              </View>
              <View style={styles.bannerActions}>
                {state.turnError.code && PLAN_ERROR.test(state.turnError.code) ? (
                  <Button label={t('chat.seePlans')} variant="secondary" onPress={() => router.push(PLANS_HREF)} />
                ) : null}
                {state.turnError.retryable ? <Button label={t('common.retry')} icon="refresh" onPress={retry} /> : null}
              </View>
            </View>
          ) : null}
        </View>
      </>
    ),
    [
      awayFromLatest,
      hasMessages,
      bannersHeight,
      jumpToLatest,
      onBannersLayout,
      state.notice,
      state.approval,
      state.turnError,
      approvalBusy,
      resolveApproval,
      retry,
      dismissError,
      colors,
      t,
    ],
  )

  // Ours, whole: the library's input toolbar is never shown. Called in place by the library, so the composer keeps
  // its text, attachments and recording across every re-render.
  const renderInputToolbar = useCallback(
    () => (
      <Composer
        onSend={onSend}
        streaming={state.streaming}
        disabled={state.phase !== 'ready'}
        // Always the full inset: the composer gives it up as the keyboard animates in (Composer.tsx). Switching it
        // here on keyboardDidHide left the composer on the navigation bar while the keyboard was closing.
        bottomInset={insets.bottom}
        sessionId={sessionId}
      />
    ),
    [onSend, state.streaming, state.phase, insets.bottom, sessionId],
  )

  return (
    <ContinueAnswerContext.Provider value={state.streaming ? null : continueAnswer}>
    <SafeAreaView edges={['top', 'left', 'right']} style={[styles.flex, { backgroundColor: colors.bg }]}>
      <AppHeader
        title={title}
        trailing={
          <ActionMenu
            label={t('chat.menu')}
            items={[{ id: 'delete', title: t('chat.deleteConversation'), icon: 'trash-outline', destructive: true }]}
            onSelect={(id) => id === 'delete' && void removeConversation()}
          />
        }
      />
      {deleteError ? <Notice tone="error">{deleteError}</Notice> : null}
      {offline ? <OfflineBanner /> : null}

      <Chat<ConversationRow>
        messages={rows}
        user={PERSON}
        locale={locale}
        colorScheme={scheme}
        theme={chatTheme}
        darkTheme={chatTheme}
        messagesContainerRef={listRef as unknown as LibraryListRef}
        listProps={listProps}
        renderMessage={renderMessage}
        renderDay={renderDay}
        isDayAnimationEnabled={false}
        renderTypingIndicator={renderNothing}
        renderChatEmpty={renderChatEmpty}
        renderChatFooter={renderChatFooter}
        renderInputToolbar={renderInputToolbar}
      />
    </SafeAreaView>
    </ContinueAnswerContext.Provider>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  centerText: { textAlign: 'center' },
  empty: { gap: space.md, paddingHorizontal: space.lg },
  // flexGrow lets an empty conversation centre its state; with messages it keeps them resting at the bottom.
  list: {
    flexGrow: 1,
    width: '100%',
    maxWidth: CONTENT_MAX_WIDTH,
    alignSelf: 'center',
    paddingHorizontal: space.lg,
    paddingVertical: space.xs,
  },
  // Half the space between two messages on each side of a row, so neighbours sit space.xl apart as before.
  row: { paddingVertical: space.md },
  jump: {
    position: 'absolute',
    right: space.lg,
    borderRadius: radius.full,
    borderWidth: StyleSheet.hairlineWidth,
  },
  banners: { width: '100%', maxWidth: CONTENT_MAX_WIDTH, alignSelf: 'center', paddingHorizontal: space.md, gap: space.sm },
  banner: { borderWidth: StyleSheet.hairlineWidth, borderRadius: radius.lg, padding: space.md, gap: space.sm, marginBottom: space.sm },
  bannerHeader: { flexDirection: 'row', alignItems: 'flex-start', gap: space.sm },
  bannerActions: { flexDirection: 'row', justifyContent: 'flex-end', gap: space.sm, flexWrap: 'wrap' },
})
