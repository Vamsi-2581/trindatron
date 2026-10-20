import Ionicons from '@expo/vector-icons/Ionicons'
import Constants from 'expo-constants'
import { router } from 'expo-router'
import { useState } from 'react'
import { Platform, Pressable, StyleSheet, TextInput, View } from 'react-native'

import {
  AppText,
  Badge,
  Button,
  Chip,
  EmptyState,
  ListRow,
  Notice,
  PRESSED_SCALE,
  Screen,
  Section,
  SkeletonRows,
  Surface,
  TextField,
} from '@/components/ui'
import { categoryIcon, categoryLabel, productPageFor, shouldOfferAssistant } from '@/features/support/helpCenter'
import { statusTone } from '@/features/support/status'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useLocaleStore, useT } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import {
  askAssistant,
  listTickets,
  openTicket,
  searchArticles,
  initialTicketCategory,
  shownSupportCategory,
  TICKET_CATEGORIES,
  type AssistantAnswer,
  type HelpArticle,
  type TicketCategory,
} from '@/services/api/support'
import { font, iconSize, radius, space, TOUCH_TARGET, useTheme } from '@/theme'

/**
 * Help center, built on the Core's real articles, categories and tickets: the question first, a search that
 * answers from the articles, the categories the Core has as a grid to browse, then a way to reach a person and the
 * tickets already open. The support assistant is not a second form any more — it is offered with the words
 * already typed, only when the articles had nothing.
 */
export default function SupportScreen() {
  const { colors } = useTheme()
  const t = useT()
  const lang = useLocaleStore((state) => state.locale)
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState<string | null>(null)
  const [answer, setAnswer] = useState<AssistantAnswer | null>(null)
  const [asking, setAsking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [composing, setComposing] = useState(false)
  const articles = useAsyncResource((signal) => searchArticles(query, lang, category, signal), [query, lang, category])
  const tickets = useAsyncResource((signal) => listTickets(signal), [], { refetchOnFocus: true })

  const results = articles.data?.articles ?? []
  const categories = (articles.data?.categories ?? []).filter(shownSupportCategory)
  const browsing = !query.trim() && category === null
  const productPage = productPageFor(category)
  const offerAssistant = shouldOfferAssistant(query, results.length, articles.loading)

  async function ask() {
    if (!query.trim() || asking) return
    setAsking(true)
    setError(null)
    try {
      setAnswer(await askAssistant(query.trim(), lang))
    } catch (cause) {
      setError(userMessage(cause))
    } finally {
      setAsking(false)
    }
  }

  function search(next: string) {
    setQuery(next)
    setAnswer(null)
  }

  return (
    <Screen>
      <View style={styles.hero}>
        <AppText variant="screenTitle" accessibilityRole="header">
          {t('support.heroTitle')}
        </AppText>
        <View style={[styles.search, { backgroundColor: colors.inputBackground, borderColor: colors.border }]}>
          <Ionicons name="search" size={iconSize.sm} color={colors.textMuted} />
          <TextInput
            value={query}
            onChangeText={search}
            placeholder={t('support.searchPlaceholder')}
            placeholderTextColor={colors.textMuted}
            accessibilityLabel={t('support.searchLabel')}
            autoCorrect={false}
            returnKeyType="search"
            style={[styles.searchInput, { color: colors.textPrimary }, Platform.OS === 'web' ? ({ outlineStyle: 'none' } as object) : null]}
          />
        </View>
      </View>

      {browsing ? (
        <Section title={t('support.browse')}>
          {articles.loading && !articles.data ? (
            <SkeletonRows count={3} icon />
          ) : (
            // The categories are the Core's own list, so the grid never offers one the help center lacks.
            <View style={styles.grid}>
              {categories.map((item) => (
                <Pressable
                  key={item}
                  accessibilityRole="button"
                  accessibilityLabel={categoryLabel(item, t)}
                  onPress={() => setCategory(item)}
                  style={({ pressed }) => [
                    styles.card,
                    {
                      backgroundColor: pressed ? colors.surface2 : colors.elevatedSurface,
                      borderColor: colors.borderSubtle,
                      transform: [{ scale: pressed ? PRESSED_SCALE : 1 }],
                    },
                  ]}
                >
                  <View style={[styles.cardIcon, { backgroundColor: colors.accentMuted }]}>
                    <Ionicons name={categoryIcon(item)} size={iconSize.md} color={colors.accentText} />
                  </View>
                  <AppText variant="bodyStrong" numberOfLines={2}>
                    {categoryLabel(item, t)}
                  </AppText>
                </Pressable>
              ))}
            </View>
          )}
        </Section>
      ) : (
        <Section
          title={category ? categoryLabel(category, t) : t('support.results')}
          action={category ? <Chip role="button" icon="close" label={t('support.filter.all')} onPress={() => setCategory(null)} /> : undefined}
        >
          {productPage ? (
            <Surface>
              <ListRow icon={productPage.icon} title={t(productPage.labelKey)} onPress={() => router.push(productPage.href)} />
            </Surface>
          ) : null}
          {articles.loading && !articles.data ? (
            <SkeletonRows count={3} />
          ) : results.length > 0 ? (
            <Surface>
              {results.map((article) => (
                <ArticleItem key={article.id} article={article} />
              ))}
            </Surface>
          ) : (
            <EmptyState icon="search-outline" title={t('support.noArticles')} body={offerAssistant ? t('support.askHint') : undefined} />
          )}

          {offerAssistant ? (
            <Surface style={styles.assistant}>
              <View style={styles.assistantHeader}>
                <Ionicons name="sparkles-outline" size={iconSize.md} color={colors.accentText} />
                <AppText variant="bodyStrong" style={styles.flex}>
                  {t('support.askTitle')}
                </AppText>
              </View>
              {answer ? (
                <View style={styles.answer} accessibilityLiveRegion="polite">
                  <AppText>{answer.answer}</AppText>
                  {answer.facts.map((fact) => (
                    <AppText key={fact} variant="caption" tone="secondary">
                      {fact}
                    </AppText>
                  ))}
                  {answer.articles.map((article) => (
                    <ArticleItem key={article.id} article={article} />
                  ))}
                  {answer.suggest_ticket ? <Button label={t('support.suggestTicket')} variant="secondary" onPress={() => setComposing(true)} /> : null}
                </View>
              ) : (
                <Button label={t('support.askSubmit')} icon="sparkles-outline" loading={asking} onPress={ask} />
              )}
              {error ? <Notice tone="error">{error}</Notice> : null}
            </Surface>
          ) : null}
        </Section>
      )}

      <Section title={t('support.contact')}>
        {composing ? (
          <NewTicket
            initialCategory={initialTicketCategory(answer?.suggested_category)}
            initialDescription={query}
            onCancel={() => setComposing(false)}
            onCreated={(id) => {
              setComposing(false)
              void tickets.refresh()
              router.push({ pathname: '/support/[ticketId]', params: { ticketId: id } })
            }}
          />
        ) : (
          <Surface>
            <ListRow icon="chatbubble-ellipses-outline" title={t('support.talkToSupport')} subtitle={t('support.talkToSupportHint')} onPress={() => setComposing(true)} />
          </Surface>
        )}
      </Section>

      <Section title={t('support.tickets')}>
        {tickets.loading && !tickets.data ? (
          <SkeletonRows count={2} icon />
        ) : tickets.data && tickets.data.tickets.length > 0 ? (
          <Surface>
            {tickets.data.tickets.map((ticket) => (
              <ListRow
                key={ticket.id}
                icon="chatbox-ellipses-outline"
                title={ticket.subject}
                subtitle={categoryLabel(ticket.category, t)}
                trailing={<Badge label={t(`support.status.${ticket.status}`)} tone={statusTone(ticket.status)} />}
                onPress={() => router.push({ pathname: '/support/[ticketId]', params: { ticketId: ticket.id } })}
              />
            ))}
          </Surface>
        ) : (
          <EmptyState icon="chatbubbles-outline" title={t('support.noTickets')} />
        )}
      </Section>
    </Screen>
  )
}

function ArticleItem({ article }: { article: HelpArticle }) {
  const { colors } = useTheme()
  const [open, setOpen] = useState(false)
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ expanded: open }}
      onPress={() => setOpen((value) => !value)}
      style={({ pressed }) => [styles.article, { backgroundColor: pressed ? colors.surface2 : 'transparent' }]}
    >
      <View style={styles.articleTitle}>
        <AppText variant="bodyStrong" style={styles.flex}>
          {article.title}
        </AppText>
        <Ionicons name={open ? 'chevron-up' : 'chevron-down'} size={iconSize.sm} color={colors.textMuted} />
      </View>
      {open ? <AppText tone="secondary">{article.body}</AppText> : null}
    </Pressable>
  )
}

function NewTicket({
  initialCategory,
  initialDescription,
  onCancel,
  onCreated,
}: {
  initialCategory: TicketCategory
  initialDescription: string
  onCancel: () => void
  onCreated: (ticketId: string) => void
}) {
  const t = useT()
  const locale = useLocaleStore((state) => state.locale)
  const [category, setCategory] = useState<TicketCategory>(initialCategory)
  const [subject, setSubject] = useState('')
  const [description, setDescription] = useState(initialDescription)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit() {
    if (!subject.trim() || !description.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      const ticket = await openTicket({
        category,
        subject: subject.trim(),
        description: description.trim(),
        platform: Platform.OS === 'ios' ? 'ios' : Platform.OS === 'web' ? 'web' : 'android',
        app_version: Constants.expoConfig?.version ?? null,
        context: { screen: 'support', locale },
      })
      onCreated(ticket.id)
    } catch (cause) {
      setError(userMessage(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Surface style={styles.form}>
      <AppText variant="caption" tone="secondary">
        {t('support.category')}
      </AppText>
      <View style={styles.chips}>
        {TICKET_CATEGORIES.filter(shownSupportCategory).map((item) => (
          <Chip key={item} label={categoryLabel(item, t)} selected={item === category} onPress={() => setCategory(item)} />
        ))}
      </View>
      {/* Focused on open: the form appears at the end of the page, so the keyboard-aware scroll brings "Assunto" up
          instead of leaving it under the navigation bar where the tap that opened the form left it. */}
      <TextField label={t('support.subject')} value={subject} onChangeText={setSubject} maxLength={160} autoFocus />
      <TextField label={t('support.description')} value={description} onChangeText={setDescription} multiline maxLength={8000} />
      {error ? <Notice tone="error">{error}</Notice> : null}
      <View style={styles.actions}>
        <Button label={t('common.cancel')} variant="ghost" onPress={onCancel} />
        <Button label={t('support.submit')} icon="send-outline" loading={busy} disabled={!subject.trim() || !description.trim()} onPress={submit} />
      </View>
    </Surface>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  hero: { gap: space.md, paddingTop: space.sm },
  search: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: radius.full,
    paddingHorizontal: space.lg,
  },
  searchInput: { flex: 1, minHeight: TOUCH_TARGET + 4, fontSize: font.size.md },
  grid: { flexDirection: 'row', flexWrap: 'wrap', justifyContent: 'space-between', rowGap: space.sm },
  // Icon beside the name: stacked with space-between, each card stood 96 dp tall around an empty middle.
  card: {
    width: '48.5%',
    minHeight: 64,
    flexDirection: 'row',
    alignItems: 'center',
    borderRadius: radius.lg,
    borderWidth: StyleSheet.hairlineWidth,
    padding: space.md,
    gap: space.sm,
  },
  cardIcon: { width: 36, height: 36, borderRadius: radius.full, alignItems: 'center', justifyContent: 'center' },
  assistant: { padding: space.lg, gap: space.md },
  assistantHeader: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  answer: { gap: space.sm },
  article: { paddingHorizontal: space.lg, paddingVertical: space.md, gap: space.xs },
  articleTitle: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  form: { padding: space.lg, gap: space.md },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm },
  actions: { flexDirection: 'row', justifyContent: 'flex-end', gap: space.sm },
})
