import { router, Stack } from 'expo-router'
import { useState } from 'react'
import { StyleSheet, View } from 'react-native'

import {
  AppText,
  Button,
  EmptyState,
  ErrorState,
  IconButton,
  ListRow,
  Notice,
  Screen,
  Section,
  SkeletonRows,
  Surface,
  TextField,
} from '@/components/ui'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useT } from '@/i18n'
import { userMessage } from '@/services/api/errors'
import { createProject, listProjects } from '@/services/api/projects'
import type { ProjectList } from '@/services/api/types'
import { readCache, writeCache } from '@/services/cache'
import { space } from '@/theme'
import { formatRelativeTime } from '@/utils/dates'

export default function ProjectsScreen() {
  const t = useT()
  const resource = useAsyncResource((signal) => listProjects(signal), [], {
    initialData: () => readCache<ProjectList>('projects'),
    onData: (data) => void writeCache('projects', data),
    refetchOnFocus: true,
  })
  const [formOpen, setFormOpen] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit() {
    if (!name.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      const project = await createProject({ name: name.trim(), description: description.trim() || null })
      resource.setData((current) => ({
        projects: [project, ...(current?.projects ?? [])],
        total: (current?.total ?? 0) + 1,
      }))
      setName('')
      setDescription('')
      setFormOpen(false)
      router.push({ pathname: '/project/[projectId]', params: { projectId: project.id } })
    } catch (cause) {
      setError(userMessage(cause))
    } finally {
      setBusy(false)
    }
  }

  const projects = resource.data?.projects ?? []

  return (
    <Screen refreshing={resource.refreshing} onRefresh={resource.refresh}>
      <Stack.Screen
        options={{ headerRight: () => <IconButton icon="add" tone="accent" label={t('projects.new')} onPress={() => setFormOpen(true)} /> }}
      />
      {formOpen ? (
        <Surface style={styles.form}>
          <AppText variant="heading" accessibilityRole="header">
            {t('projects.new')}
          </AppText>
          <TextField label={t('projects.name')} value={name} onChangeText={setName} maxLength={255} autoFocus returnKeyType="next" />
          <TextField label={t('projects.description')} value={description} onChangeText={setDescription} multiline />
          {error ? <Notice tone="error">{error}</Notice> : null}
          <View style={styles.actions}>
            <Button label={t('common.cancel')} variant="ghost" onPress={() => setFormOpen(false)} />
            <Button label={t('projects.create')} loading={busy} disabled={!name.trim()} onPress={submit} />
          </View>
        </Surface>
      ) : null}

      <Section title={t('projects.yours')}>
        {resource.loading && !resource.data ? (
          <SkeletonRows count={4} icon />
        ) : resource.error && !resource.data ? (
          <ErrorState message={resource.error.message} onRetry={resource.refresh} />
        ) : projects.length === 0 ? (
          <EmptyState
            icon="folder-open-outline"
            title={t('projects.emptyTitle')}
            body={t('projects.emptyBody')}
            action={formOpen ? undefined : <Button label={t('projects.new')} icon="add" onPress={() => setFormOpen(true)} />}
          />
        ) : (
          <Surface>
            {projects.map((project) => {
              const files = project.files.length === 1 ? t('projects.filesOne') : t('projects.filesMany', { count: project.files.length })
              return (
                <ListRow
                  key={project.id}
                  icon="folder-outline"
                  title={project.name}
                  subtitle={[project.description, `${files} · ${formatRelativeTime(project.updated_at)}`].filter(Boolean).join('\n')}
                  onPress={() => router.push({ pathname: '/project/[projectId]', params: { projectId: project.id } })}
                />
              )
            })}
          </Surface>
        )}
      </Section>
    </Screen>
  )
}

const styles = StyleSheet.create({
  form: { padding: space.lg, gap: space.md },
  actions: { flexDirection: 'row', justifyContent: 'flex-end', gap: space.sm },
})
