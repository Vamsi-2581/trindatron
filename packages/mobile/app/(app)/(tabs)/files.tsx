import Ionicons from '@expo/vector-icons/Ionicons'
import { router, Stack } from 'expo-router'
import { useMemo, useState } from 'react'
import { Platform, ScrollView, StyleSheet, TextInput, View } from 'react-native'

import { AttachmentSheet } from '@/components/chat/AttachmentSheet'
import { FileRow, UploadList } from '@/components/files/FileComponents'
import {
  AppText,
  BottomSheet,
  Button,
  Chip,
  EmptyState,
  ErrorState,
  IconButton,
  Notice,
  Screen,
  SheetOption,
  SkeletonRows,
  Surface,
} from '@/components/ui'
import { buildFileLibrary, type FileKind, type LibraryFile } from '@/features/files/library'
import { useProjectUploads } from '@/features/files/useProjectUploads'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useT } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import { deleteProjectFile, listProjects } from '@/services/api/projects'
import type { ProjectList } from '@/services/api/types'
import { readCache, writeCache } from '@/services/cache'
import { font, iconSize, radius, space, TOUCH_TARGET, useTheme } from '@/theme'
import { confirmAction } from '@/utils/confirm'

const KINDS: FileKind[] = ['all', 'images', 'documents']

/**
 * The user's file library across every project: search, filter by type and
 * project, open the project a file belongs to, upload, delete. The Core has no
 * file preview, download or global file store yet (docs/MOBILE.md §10), so
 * none of those are offered here.
 */
export default function FilesScreen() {
  const { colors } = useTheme()
  const t = useT()
  const resource = useAsyncResource((signal) => listProjects(signal), [], {
    initialData: () => readCache<ProjectList>('projects'),
    onData: (data) => void writeCache('projects', data),
    refetchOnFocus: true,
  })
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState<FileKind>('all')
  const [projectFilter, setProjectFilter] = useState<string | null>(null)
  const [projectSheet, setProjectSheet] = useState(false)
  const [uploadSheet, setUploadSheet] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  const uploads = useProjectUploads((file) =>
    resource.setData((current) =>
      current
        ? {
            ...current,
            projects: current.projects.map((project) =>
              project.id === file.project_id ? { ...project, files: [...project.files, file] } : project,
            ),
          }
        : current,
    ),
  )

  const projects = useMemo(() => resource.data?.projects ?? [], [resource.data])
  const library = useMemo(() => buildFileLibrary(projects, { query, kind, projectId: projectFilter }), [projects, query, kind, projectFilter])
  const hasAnyFile = projects.some((project) => project.files.length > 0)
  const filteredProject = projects.find((project) => project.id === projectFilter) ?? null

  async function remove(file: LibraryFile) {
    const confirmed = await confirmAction({ title: t('files.deleteTitle'), message: file.name, confirmLabel: t('common.delete'), destructive: true })
    if (!confirmed) return
    try {
      await deleteProjectFile(file.project_id, file.id)
      resource.setData((current) =>
        current
          ? {
              ...current,
              projects: current.projects.map((project) =>
                project.id === file.project_id ? { ...project, files: project.files.filter((item) => item.id !== file.id) } : project,
              ),
            }
          : current,
      )
    } catch (cause) {
      setNotice(userMessage(cause))
    }
  }

  if (resource.loading && !resource.data) {
    return (
      <Screen>
        <SkeletonRows count={5} icon />
      </Screen>
    )
  }
  if (resource.error && !resource.data) {
    return (
      <Screen>
        <ErrorState message={resource.error.message} onRetry={resource.refresh} />
      </Screen>
    )
  }
  if (projects.length === 0) {
    return (
      <Screen>
        <EmptyState
          icon="folder-open-outline"
          title={t('files.needProjectTitle')}
          body={t('files.needProjectBody')}
          action={<Button label={t('files.goToProjects')} onPress={() => router.push('/projects')} />}
        />
      </Screen>
    )
  }

  return (
    <Screen refreshing={resource.refreshing} onRefresh={resource.refresh} contentStyle={styles.content}>
      <Stack.Screen
        options={{
          headerRight: () => <IconButton icon="cloud-upload-outline" label={t('files.upload')} tone="accent" onPress={() => setUploadSheet(true)} />,
        }}
      />
      <View style={styles.searchRow}>
        <View style={[styles.search, { backgroundColor: colors.inputBackground, borderColor: colors.border }]}>
          <Ionicons name="search" size={iconSize.sm} color={colors.textMuted} />
          <TextInput
            value={query}
            onChangeText={setQuery}
            placeholder={t('files.search')}
            placeholderTextColor={colors.textMuted}
            accessibilityLabel={t('files.search')}
            autoCorrect={false}
            returnKeyType="search"
            style={[styles.searchInput, { color: colors.textPrimary }, Platform.OS === 'web' ? ({ outlineStyle: 'none' } as object) : null]}
          />
        </View>
      </View>

      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.chips}>
        {KINDS.map((item) => (
          <Chip key={item} label={t(`files.kind.${item}`)} selected={kind === item} onPress={() => setKind(item)} />
        ))}
        <Chip
          role="button"
          trailingIcon="chevron-down"
          label={filteredProject ? t('files.project', { name: filteredProject.name }) : t('files.allProjects')}
          accessibilityLabel={t('files.chooseProject')}
          selected={filteredProject !== null}
          onPress={() => setProjectSheet(true)}
        />
      </ScrollView>

      {notice ? <Notice tone="error">{notice}</Notice> : null}
      <UploadList items={uploads.items} onCancel={uploads.cancel} onRetry={uploads.retry} onDismiss={uploads.dismiss} />

      {!hasAnyFile ? (
        <EmptyState icon="document-outline" title={t('files.emptyTitle')} body={t('files.emptyBody')} />
      ) : library.length === 0 ? (
        <AppText variant="caption" tone="muted" style={styles.center}>
          {t('files.noResults')}
        </AppText>
      ) : (
        <Surface>
          {library.map((file) => (
            <FileRow
              key={file.id}
              file={file}
              projectName={file.projectName}
              onOpen={() => router.push({ pathname: '/project/[projectId]', params: { projectId: file.project_id } })}
              onDelete={() => remove(file)}
            />
          ))}
        </Surface>
      )}

      <AppText variant="caption" tone="muted" style={styles.center}>
        {t('files.limit')}
      </AppText>

      <BottomSheet visible={projectSheet} onClose={() => setProjectSheet(false)} title={t('files.chooseProject')}>
        <SheetOption
          label={t('files.allProjects')}
          selected={projectFilter === null}
          onPress={() => {
            setProjectFilter(null)
            setProjectSheet(false)
          }}
        />
        {projects.map((project) => (
          <SheetOption
            key={project.id}
            icon="folder-outline"
            label={project.name}
            selected={projectFilter === project.id}
            onPress={() => {
              setProjectFilter(project.id)
              setProjectSheet(false)
            }}
          />
        ))}
      </BottomSheet>

      <AttachmentSheet
        visible={uploadSheet}
        onClose={() => setUploadSheet(false)}
        projectId={projectFilter}
        creations={[]}
        onCreate={() => undefined}
        onPicked={(file, project) => uploads.start(project.id, file)}
      />
    </Screen>
  )
}

const styles = StyleSheet.create({
  content: { gap: space.md },
  center: { textAlign: 'center' },
  searchRow: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  search: { flex: 1, flexDirection: 'row', alignItems: 'center', gap: space.sm, borderWidth: StyleSheet.hairlineWidth, borderRadius: radius.full, paddingHorizontal: space.md },
  searchInput: { flex: 1, minHeight: TOUCH_TARGET - 4, fontSize: font.size.md },
  chips: { gap: space.sm },
})
