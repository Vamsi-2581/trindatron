import { router, Stack, useLocalSearchParams } from 'expo-router'
import { useState } from 'react'
import { StyleSheet, View } from 'react-native'

import { FileRow, UploadList, UploadSourceBar } from '@/components/files/FileComponents'
import {
  AppText,
  Button,
  ErrorState,
  ListRow,
  SkeletonRows,
  Notice,
  Screen,
  Section,
  Surface,
  TextField,
} from '@/components/ui'
import { StartConversation } from '@/features/chat/StartConversation'
import { useProjectUploads } from '@/features/files/useProjectUploads'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useT } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import { archiveProject, deleteProjectFile, getProject, listProjectSessions, updateProject } from '@/services/api/projects'
import type { ProjectFile } from '@/services/api/types'
import { space } from '@/theme'
import { confirmAction } from '@/utils/confirm'
import { formatRelativeTime } from '@/utils/dates'
import { conversationTitle } from '@/utils/format'

export default function ProjectScreen() {
  const t = useT()
  const { projectId } = useLocalSearchParams<{ projectId: string }>()
  const project = useAsyncResource((signal) => getProject(projectId, signal), [projectId])
  const sessions = useAsyncResource((signal) => listProjectSessions(projectId, signal), [projectId], { refetchOnFocus: true })
  const uploads = useProjectUploads((file) => project.setData((current) => (current ? { ...current, files: [...current.files, file] } : current)))

  const [editing, setEditing] = useState(false)
  const [instructions, setInstructions] = useState('')
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  if (project.loading && !project.data) {
    return (
      <Screen edges={['bottom', 'left', 'right']}>
        <SkeletonRows count={4} />
      </Screen>
    )
  }
  if (!project.data) {
    return (
      <Screen edges={['bottom', 'left', 'right']}>
        <ErrorState message={project.error?.message ?? t('project.notFound')} onRetry={project.refresh} />
      </Screen>
    )
  }
  const data = project.data

  async function saveInstructions() {
    setSaving(true)
    setNotice(null)
    try {
      project.setData(await updateProject(data.id, { instructions: instructions.trim() || null }))
      setEditing(false)
    } catch (cause) {
      setNotice(userMessage(cause))
    } finally {
      setSaving(false)
    }
  }

  async function removeFile(file: ProjectFile) {
    const confirmed = await confirmAction({ title: t('files.deleteTitle'), message: file.name, confirmLabel: t('common.delete'), destructive: true })
    if (!confirmed) return
    try {
      await deleteProjectFile(data.id, file.id)
      project.setData((current) => (current ? { ...current, files: current.files.filter((item) => item.id !== file.id) } : current))
    } catch (cause) {
      setNotice(userMessage(cause))
    }
  }

  async function archive() {
    const confirmed = await confirmAction({
      title: t('project.archiveTitle'),
      message: t('project.archiveMessage', { name: data.name }),
      confirmLabel: t('project.archive'),
      destructive: true,
    })
    if (!confirmed) return
    try {
      await archiveProject(data.id)
      router.back()
    } catch (cause) {
      setNotice(userMessage(cause))
    }
  }

  const projectSessions = sessions.data?.sessions ?? []

  return (
    <Screen
      edges={['bottom', 'left', 'right']}
      refreshing={project.refreshing}
      onRefresh={() => {
        void project.refresh()
        void sessions.refresh()
      }}
    >
      <Stack.Screen options={{ title: data.name }} />
      <View style={styles.header}>
        <AppText variant="title" accessibilityRole="header">
          {data.name}
        </AppText>
        {data.description ? <AppText tone="secondary">{data.description}</AppText> : null}
      </View>

      {notice ? <Notice tone="error">{notice}</Notice> : null}

      <StartConversation projectId={data.id} placeholder={t('start.projectPlaceholder', { name: data.name })} />

      <Section title={t('project.conversations')}>
        {sessions.loading && !sessions.data ? (
          <SkeletonRows count={4} />
        ) : projectSessions.length === 0 ? (
          <AppText variant="caption" tone="muted">
            {t('project.noConversations')}
          </AppText>
        ) : (
          <Surface>
            {projectSessions.map((session) => (
              <ListRow
                key={session.id}
                icon="chatbubble-ellipses-outline"
                title={conversationTitle(session.title)}
                subtitle={formatRelativeTime(session.updated_at)}
                onPress={() => router.push({ pathname: '/chat/[sessionId]', params: { sessionId: session.id } })}
              />
            ))}
          </Surface>
        )}
      </Section>

      <Section
        title={t('project.instructions')}
        action={
          editing ? null : (
            <Button
              label={t('common.edit')}
              variant="ghost"
              icon="create-outline"
              onPress={() => {
                setInstructions(data.instructions ?? '')
                setEditing(true)
              }}
            />
          )
        }
      >
        {editing ? (
          <View style={styles.editor}>
            <TextField label={t('project.instructionsLabel')} value={instructions} onChangeText={setInstructions} multiline />
            <View style={styles.actions}>
              <Button label={t('common.cancel')} variant="ghost" onPress={() => setEditing(false)} />
              <Button label={t('common.save')} loading={saving} onPress={saveInstructions} />
            </View>
          </View>
        ) : (
          <AppText tone={data.instructions ? 'primary' : 'muted'} variant={data.instructions ? 'body' : 'caption'}>
            {data.instructions || t('project.noInstructions')}
          </AppText>
        )}
      </Section>

      <Section title={t('project.files')}>
        <UploadSourceBar onPicked={(file) => uploads.start(data.id, file)} />
        <UploadList items={uploads.items} onCancel={uploads.cancel} onRetry={uploads.retry} onDismiss={uploads.dismiss} />
        {data.files.length > 0 ? (
          <Surface>
            {data.files.map((file) => (
              <FileRow key={file.id} file={file} onDelete={() => removeFile(file)} />
            ))}
          </Surface>
        ) : (
          <AppText variant="caption" tone="muted">
            {t('project.noFiles')}
          </AppText>
        )}
      </Section>

      <Button label={t('project.archive')} variant="danger" icon="archive-outline" onPress={archive} />
    </Screen>
  )
}

const styles = StyleSheet.create({
  header: { gap: space.xs },
  editor: { gap: space.md },
  actions: { flexDirection: 'row', justifyContent: 'flex-end', gap: space.sm },
})
