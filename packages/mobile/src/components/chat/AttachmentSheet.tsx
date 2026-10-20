import { router } from 'expo-router'
import { useState } from 'react'
import { ScrollView, StyleSheet, View } from 'react-native'

import { AppText, BottomSheet, Button, Chip, SheetOption, type IconName } from '@/components/ui'
import type { MediaCapability } from '@/features/capabilities/capabilities'
import { PermissionDeniedError, pickDocument, pickImageFromLibrary, takePhoto, type PickedFile } from '@/features/files/pickers'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useT } from '@/i18n'
import { listProjects } from '@/services/api/projects'
import type { ProjectList } from '@/services/api/types'
import { readCache, writeCache } from '@/services/cache'
import { space } from '@/theme'

export const CREATION_ENTRIES: Record<MediaCapability, { icon: IconName; labelKey: string; promptKey: string }> = {
  image_generation: { icon: 'color-palette-outline', labelKey: 'composer.create.image', promptKey: 'composer.prompt.image' },
  image_editing: { icon: 'brush-outline', labelKey: 'composer.create.editImage', promptKey: 'composer.prompt.editImage' },
  video_generation: { icon: 'film-outline', labelKey: 'composer.create.video', promptKey: 'composer.prompt.video' },
  image_to_video: { icon: 'play-circle-outline', labelKey: 'composer.create.imageToVideo', promptKey: 'composer.prompt.imageToVideo' },
  avatar_generation: { icon: 'person-circle-outline', labelKey: 'composer.create.avatar', promptKey: 'composer.prompt.avatar' },
}

export interface AttachmentSheetProps {
  visible: boolean
  onClose: () => void
  creations: MediaCapability[]
  onCreate: (capability: MediaCapability) => void
  /** In a conversation (the composer): a picked file is attached to it. Cancelling the picker attaches nothing. */
  onAttach?: (file: PickedFile) => void
  /** In the file library: the project files are saved to. Without one the user picks a destination. */
  projectId?: string | null
  onPicked?: (file: PickedFile, project: { id: string; name: string }) => void
}

export function AttachmentSheet(props: AttachmentSheetProps) {
  return (
    <BottomSheet visible={props.visible} onClose={props.onClose}>
      {props.visible ? <AttachmentOptions {...props} /> : null}
    </BottomSheet>
  )
}

function AttachmentOptions({ onClose, projectId = null, creations, onCreate, onPicked, onAttach }: AttachmentSheetProps) {
  const t = useT()
  const [destination, setDestination] = useState<string | null>(projectId)
  const [error, setError] = useState<string | null>(null)
  const projects = useAsyncResource((signal) => (onAttach ? Promise.resolve(null) : listProjects(signal)), [], {
    initialData: () => (onAttach ? Promise.resolve(null) : readCache<ProjectList>('projects')),
    onData: (data) => void (data ? writeCache('projects', data) : undefined),
  })
  const list = projects.data?.projects ?? []
  const target = list.find((project) => project.id === (destination ?? list[0]?.id)) ?? null
  const canPick = Boolean(onAttach) || target !== null

  async function pick(source: () => Promise<PickedFile | null>) {
    if (!canPick) return
    setError(null)
    try {
      const file = await source()
      if (!file) return
      onClose()
      if (onAttach) onAttach(file)
      else if (target && onPicked) onPicked(file, { id: target.id, name: target.name })
    } catch (cause) {
      setError(cause instanceof PermissionDeniedError ? cause.message : t('upload.pickerFailed'))
    }
  }

  const savesTo = onAttach ? t('composer.attachHere') : target ? t('composer.savesTo', { name: target.name }) : null

  return (
    <View>
      {creations.length > 0 ? (
        <AppText variant="sectionTitle" accessibilityRole="header" style={styles.section}>
          {t('composer.sectionCreate')}
        </AppText>
      ) : null}
      {creations.map((capability) => {
        const entry = CREATION_ENTRIES[capability]
        return (
          <SheetOption
            key={capability}
            icon={entry.icon}
            label={t(entry.labelKey)}
            onPress={() => {
              onClose()
              onCreate(capability)
            }}
          />
        )
      })}

      {!onAttach && !projectId && list.length > 1 ? (
        <View style={styles.destination}>
          <AppText variant="caption" tone="muted">
            {t('files.destination')}
          </AppText>
          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.chips}>
            {list.map((project) => {
              const selected = project.id === target?.id
              return (
                <Chip
                  key={project.id}
                  label={project.name}
                  selected={selected}
                  accessibilityLabel={t('files.destinationA11y', { name: project.name })}
                  onPress={() => setDestination(project.id)}
                />
              )
            })}
          </ScrollView>
        </View>
      ) : null}

      <AppText variant="sectionTitle" accessibilityRole="header" style={styles.section}>
        {t('composer.add')}
      </AppText>
      <SheetOption icon="camera-outline" label={t('upload.camera')} detail={savesTo} disabled={!canPick} onPress={() => pick(takePhoto)} />
      <SheetOption icon="images-outline" label={t('upload.gallery')} detail={savesTo} disabled={!canPick} onPress={() => pick(pickImageFromLibrary)} />
      <SheetOption icon="document-outline" label={t('upload.document')} detail={savesTo} disabled={!canPick} onPress={() => pick(pickDocument)} />

      {!onAttach && !projects.loading && list.length === 0 ? (
        <View style={styles.noProject}>
          <AppText variant="caption" tone="secondary">
            {t('composer.needProject')}
          </AppText>
          <Button
            label={t('files.goToProjects')}
            variant="secondary"
            onPress={() => {
              onClose()
              router.push('/projects')
            }}
          />
        </View>
      ) : null}

      {error ? (
        <AppText variant="caption" tone="error" accessibilityLiveRegion="polite" style={styles.error}>
          {error}
        </AppText>
      ) : null}
    </View>
  )
}

const styles = StyleSheet.create({
  section: { paddingHorizontal: space.md, paddingTop: space.sm, paddingBottom: space.xs },
  destination: { paddingHorizontal: space.md, paddingVertical: space.sm, gap: space.xs },
  chips: { gap: space.sm },
  noProject: { padding: space.md, gap: space.sm, alignItems: 'flex-start' },
  error: { paddingHorizontal: space.md, paddingTop: space.sm },
})
