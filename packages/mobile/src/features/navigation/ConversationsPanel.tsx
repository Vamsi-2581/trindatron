import Ionicons from '@expo/vector-icons/Ionicons'
import { router, usePathname, type Href } from 'expo-router'
import { useMemo, useState } from 'react'
import { Platform, Pressable, RefreshControl, SectionList, StyleSheet, TextInput, View } from 'react-native'

import { ActionMenu, AppText, BrandSymbol, Badge, IconButton, Notice, SkeletonRows } from '@/components/ui'
import { useCapabilityStore } from '@/features/capabilities/store'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useT } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import { deleteSession, listSessions } from '@/services/api/sessions'
import type { Session, SessionList } from '@/services/api/types'
import { readCache, writeCache } from '@/services/cache'
import { useAuthStore } from '@/stores/auth'
import { font, iconSize, radius, space, TOUCH_TARGET, useTheme } from '@/theme'
import { confirmAction } from '@/utils/confirm'
import { conversationTitle, firstName, planLabel } from '@/utils/format'

import { groupConversations } from './conversationGroups'
import { SETTINGS_HREF, SUPPORT_HREF } from './routes'
import { ACCOUNT_PLACES, visibleTools, type Tool } from './tools'

function normalize(value: string): string {
  return value.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase()
}

/**
 * The side menu, laid out the way AI products have settled on (LibreChat, MIT, is the reference): a compact header
 * with a new-conversation action, search, the history grouped by when it was last used, the tools this account
 * has, and the account itself at the foot.
 *
 * A new conversation opens Home, which starts one on the first message — nothing is created on the Core until
 * there is something to say. Conversations can be deleted from their menu; renaming waits for a Core endpoint.
 */
export function ConversationsPanel({ onNavigate, onClose }: { onNavigate: () => void; onClose?: () => void }) {
  const { colors } = useTheme()
  const t = useT()
  const pathname = usePathname()
  const user = useAuthStore((state) => state.user)
  const capabilities = useCapabilityStore((state) => state.capabilities)
  const [query, setQuery] = useState('')
  const [notice, setNotice] = useState<string | null>(null)
  const resource = useAsyncResource((signal) => listSessions(signal), [], {
    initialData: () => readCache<SessionList>('sessions'),
    onData: (data) => void writeCache('sessions', data),
  })

  const sections = useMemo(() => {
    const all = resource.data?.sessions ?? []
    const needle = normalize(query.trim())
    const matching = needle ? all.filter((session) => normalize(conversationTitle(session.title)).includes(needle)) : all
    return groupConversations(matching).map((group) => ({ key: group.key, title: t(`chats.groups.${group.key}`), data: group.sessions }))
  }, [resource.data, query, t])
  const tools = useMemo(() => visibleTools(capabilities), [capabilities])

  function go(href: Href) {
    onNavigate()
    router.push(href)
  }

  function open(session: Session) {
    onNavigate()
    router.push({ pathname: '/chat/[sessionId]', params: { sessionId: session.id } })
  }

  function newConversation() {
    onNavigate()
    if (pathname !== '/') router.navigate('/')
  }

  async function remove(session: Session) {
    const title = conversationTitle(session.title)
    const confirmed = await confirmAction({
      title: t('chats.deleteTitle'),
      message: t('chats.deleteMessage', { title }),
      confirmLabel: t('common.delete'),
      destructive: true,
    })
    if (!confirmed) return
    setNotice(null)
    try {
      await deleteSession(session.id)
      resource.setData((current) =>
        current ? { sessions: current.sessions.filter((item) => item.id !== session.id), total: current.total - 1 } : current,
      )
      if (pathname === `/chat/${session.id}`) router.replace('/')
    } catch (cause) {
      setNotice(userMessage(cause))
    }
  }

  const name = user?.full_name?.trim() || user?.email || ''
  const loadingFirstTime = resource.loading && !resource.data

  return (
    <View style={styles.flex}>
      <View style={styles.header}>
        <BrandSymbol size={24} />
        <AppText variant="headerTitle" accessibilityRole="header" style={styles.flex}>
          {t('nav.chats')}
        </AppText>
        <IconButton icon="create-outline" label={t('chats.new')} size="md" onPress={newConversation} />
        {onClose ? <IconButton icon="close" label={t('drawer.close')} size="md" onPress={onClose} /> : null}
      </View>

      <View style={styles.searchWrap}>
        <View style={[styles.search, { backgroundColor: colors.inputBackground, borderColor: colors.border }]}>
          <Ionicons name="search" size={iconSize.sm} color={colors.textMuted} />
          <TextInput
            value={query}
            onChangeText={setQuery}
            placeholder={t('chats.search')}
            placeholderTextColor={colors.textMuted}
            accessibilityLabel={t('chats.search')}
            autoCorrect={false}
            returnKeyType="search"
            style={[styles.searchInput, { color: colors.textPrimary }, Platform.OS === 'web' ? ({ outlineStyle: 'none' } as object) : null]}
          />
        </View>
        {notice ? <Notice tone="error">{notice}</Notice> : null}
      </View>

      <SectionList
        sections={sections}
        keyExtractor={(item) => item.id}
        style={styles.flex}
        showsVerticalScrollIndicator={false}
        contentContainerStyle={styles.list}
        keyboardShouldPersistTaps="handled"
        stickySectionHeadersEnabled={false}
        refreshControl={<RefreshControl refreshing={resource.refreshing} onRefresh={resource.refresh} tintColor={colors.accent} colors={[colors.accent]} />}
        ListEmptyComponent={
          loadingFirstTime ? (
            <SkeletonRows count={6} />
          ) : resource.error && !resource.data ? (
            <Notice tone="error">{resource.error.message}</Notice>
          ) : (
            <AppText variant="caption" tone="muted" style={styles.empty}>
              {query.trim() ? t('chats.noResults') : t('chats.emptyBody')}
            </AppText>
          )
        }
        renderSectionHeader={({ section }) => (
          <AppText variant="sectionTitle" accessibilityRole="header" style={styles.groupTitle}>
            {section.title}
          </AppText>
        )}
        renderItem={({ item }) => {
          const title = conversationTitle(item.title)
          const active = pathname === `/chat/${item.id}`
          return (
            <View style={[styles.row, { backgroundColor: active ? colors.surface3 : 'transparent' }]}>
              <Pressable
                accessibilityRole="button"
                accessibilityLabel={title}
                accessibilityHint={t('chats.openHint')}
                accessibilityState={{ selected: active }}
                accessibilityActions={[{ name: 'delete', label: t('chats.deleteA11y', { title }) }]}
                onAccessibilityAction={(event) => event.nativeEvent.actionName === 'delete' && void remove(item)}
                onPress={() => open(item)}
                style={({ pressed }) => [styles.rowMain, { opacity: pressed ? 0.6 : 1 }]}
              >
                <AppText variant="body" numberOfLines={1}>
                  {title}
                </AppText>
              </Pressable>
              <ActionMenu
                label={t('chats.actions', { title })}
                items={[{ id: 'delete', title: t('chat.deleteConversation'), icon: 'trash-outline', destructive: true }]}
                onSelect={(id) => id === 'delete' && void remove(item)}
              />
            </View>
          )
        }}
        ListFooterComponent={
          <>
            {tools.length > 0 ? <PlaceGroup title={t('nav.tools')} places={tools} onOpen={go} /> : null}
            <PlaceGroup title={t('nav.account')} places={ACCOUNT_PLACES} onOpen={go} />
          </>
        }
      />

      <View style={[styles.account, { borderTopColor: colors.separator }]}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={t('nav.profile')}
          accessibilityHint={name}
          onPress={() => go('/profile')}
          style={({ pressed }) => [styles.accountMain, { backgroundColor: pressed ? colors.surface2 : 'transparent' }]}
        >
          <View style={[styles.avatar, { backgroundColor: colors.accentMuted }]}>
            <AppText variant="bodyStrong" tone="accent">
              {(firstName(user?.full_name) ?? user?.email ?? '?').charAt(0).toUpperCase()}
            </AppText>
          </View>
          <View style={styles.flex}>
            <AppText variant="bodyStrong" numberOfLines={1}>
              {name}
            </AppText>
            {user?.plan ? <Badge label={t('home.plan', { plan: planLabel(user.plan) })} tone="accent" /> : null}
          </View>
        </Pressable>
        <IconButton icon="help-buoy-outline" label={t('nav.support')} size="md" onPress={() => go(SUPPORT_HREF)} />
        <IconButton icon="settings-outline" label={t('nav.settings')} size="md" onPress={() => go(SETTINGS_HREF)} />
      </View>
    </View>
  )
}

/** A titled group of places in the side menu; each row opens its own screen. */
function PlaceGroup({ title, places, onOpen }: { title: string; places: Tool[]; onOpen: (href: Tool['href']) => void }) {
  const { colors } = useTheme()
  const t = useT()
  return (
    <View style={[styles.tools, { borderTopColor: colors.separator }]}>
      <AppText variant="sectionTitle" accessibilityRole="header" style={styles.groupTitle}>
        {title}
      </AppText>
      {places.map((place) => (
        <Pressable
          key={place.key}
          accessibilityRole="button"
          accessibilityLabel={t(place.labelKey)}
          onPress={() => onOpen(place.href)}
          style={({ pressed }) => [styles.tool, { backgroundColor: pressed ? colors.surface2 : 'transparent' }]}
        >
          <Ionicons name={place.icon} size={iconSize.md} color={colors.textSecondary} />
          <AppText variant="body" numberOfLines={1} style={styles.flex}>
            {t(place.labelKey)}
          </AppText>
        </Pressable>
      ))}
    </View>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  header: { flexDirection: 'row', alignItems: 'center', gap: space.sm, paddingLeft: space.lg, paddingRight: space.xs, minHeight: TOUCH_TARGET + space.sm },
  searchWrap: { paddingHorizontal: space.md, paddingBottom: space.sm, gap: space.sm },
  search: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: radius.full,
    paddingHorizontal: space.md,
  },
  searchInput: { flex: 1, minHeight: 38, fontSize: font.size.sm },
  list: { paddingHorizontal: space.sm, paddingBottom: space.md },
  groupTitle: { paddingHorizontal: space.md, paddingTop: space.md, paddingBottom: space.xs },
  row: { flexDirection: 'row', alignItems: 'center', borderRadius: radius.md, paddingLeft: space.md, minHeight: TOUCH_TARGET },
  rowMain: { flex: 1, minHeight: TOUCH_TARGET, justifyContent: 'center' },
  empty: { padding: space.lg, textAlign: 'center' },
  tools: { marginTop: space.md, borderTopWidth: StyleSheet.hairlineWidth },
  tool: { minHeight: TOUCH_TARGET, flexDirection: 'row', alignItems: 'center', gap: space.md, paddingHorizontal: space.md, borderRadius: radius.md },
  account: { flexDirection: 'row', alignItems: 'center', gap: space.xs, paddingHorizontal: space.sm, paddingVertical: space.sm, borderTopWidth: StyleSheet.hairlineWidth },
  accountMain: { flex: 1, flexDirection: 'row', alignItems: 'center', gap: space.md, paddingHorizontal: space.sm, paddingVertical: space.xs, borderRadius: radius.md, minHeight: TOUCH_TARGET },
  avatar: { width: 36, height: 36, borderRadius: radius.full, alignItems: 'center', justifyContent: 'center' },
})
