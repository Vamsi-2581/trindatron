import { router, Stack } from 'expo-router'
import { useState } from 'react'
import { FlatList, RefreshControl, StyleSheet, View } from 'react-native'
import { SafeAreaView } from 'react-native-safe-area-context'

import { MediaResultCard } from '@/components/media/MediaResultCard'
import { Button, Chip, CONTENT_MAX_WIDTH, EmptyState, ErrorState, IconButton, Notice, SkeletonRows } from '@/components/ui'
import { useCapabilityStore } from '@/features/capabilities/store'
import { creationFromJob } from '@/features/media/media'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useT } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import { deleteCreation, listCreations, type CreationList } from '@/services/api/media'
import { space, useTheme } from '@/theme'
import { confirmAction } from '@/utils/confirm'

type Filter = 'all' | 'image' | 'video'
const FILTERS: Filter[] = ['all', 'image', 'video']

/** Every image and video this account created, newest first — the same list Web and Desktop show. */
export default function CreationsScreen() {
  const { colors } = useTheme()
  const t = useT()
  const [filter, setFilter] = useState<Filter>('all')
  const [notice, setNotice] = useState<string | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  // Creating is offered only while the Core says an image or a video can be made right now.
  const createKind = useCapabilityStore((state) =>
    state.capabilities.image_generation.available ? 'image' : state.capabilities.video_generation.available ? 'video' : null,
  )
  const resource = useAsyncResource((signal) => listCreations({ kind: filter === 'all' ? null : filter }, signal), [filter], {
    refetchOnFocus: true,
  })

  function create() {
    if (createKind) router.push({ pathname: '/create', params: { kind: createKind } })
  }

  async function loadMore() {
    const before = resource.data?.next_before
    if (!before || loadingMore) return
    setLoadingMore(true)
    try {
      const page = await listCreations({ kind: filter === 'all' ? null : filter, before })
      resource.setData((current: CreationList | null) =>
        current ? { creations: [...current.creations, ...page.creations], next_before: page.next_before } : page,
      )
    } catch (cause) {
      setNotice(userMessage(cause))
    } finally {
      setLoadingMore(false)
    }
  }

  async function remove(id: string) {
    const confirmed = await confirmAction({ title: t('creations.deleteTitle'), message: t('creations.deleteMessage'), confirmLabel: t('common.delete'), destructive: true })
    if (!confirmed) return
    try {
      await deleteCreation(id)
      resource.setData((current) => (current ? { ...current, creations: current.creations.filter((item) => item.id !== id) } : current))
    } catch (cause) {
      setNotice(userMessage(cause))
    }
  }

  return (
    <SafeAreaView edges={['left', 'right', 'bottom']} style={[styles.flex, { backgroundColor: colors.bg }]}>
      <Stack.Screen
        options={{
          headerRight: createKind ? () => <IconButton icon="add" tone="accent" label={t('creations.create')} onPress={create} /> : undefined,
        }}
      />
      <FlatList
        data={resource.data?.creations ?? []}
        keyExtractor={(item) => item.id}
        contentContainerStyle={styles.list}
        refreshControl={<RefreshControl refreshing={resource.refreshing} onRefresh={resource.refresh} tintColor={colors.accent} colors={[colors.accent]} />}
        ListHeaderComponent={
          <View style={styles.header}>
            <View style={styles.chips}>
              {FILTERS.map((item) => (
                <Chip key={item} label={t(`creations.filter.${item}`)} selected={filter === item} onPress={() => setFilter(item)} />
              ))}
            </View>
            {notice ? <Notice tone="error">{notice}</Notice> : null}
          </View>
        }
        ListEmptyComponent={
          resource.loading ? (
            <SkeletonRows count={3} />
          ) : resource.error ? (
            <ErrorState message={resource.error.message} onRetry={resource.refresh} />
          ) : (
            <EmptyState
              icon="images-outline"
              title={t('creations.emptyTitle')}
              body={t('creations.emptyBody')}
              action={createKind ? <Button label={t('creations.create')} icon="add" onPress={create} /> : undefined}
            />
          )
        }
        renderItem={({ item }) => <MediaResultCard media={creationFromJob(item)} onDelete={() => void remove(item.id)} />}
        ListFooterComponent={
          resource.data?.next_before ? <Button label={t('creations.more')} variant="secondary" loading={loadingMore} onPress={loadMore} /> : null
        }
      />
    </SafeAreaView>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  list: { width: '100%', maxWidth: CONTENT_MAX_WIDTH, alignSelf: 'center', padding: space.lg, gap: space.lg },
  header: { gap: space.md },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm },
})
