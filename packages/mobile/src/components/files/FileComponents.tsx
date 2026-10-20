import Ionicons from '@expo/vector-icons/Ionicons'
import { Image } from 'expo-image'
import { useState } from 'react'
import { Pressable, StyleSheet, View } from 'react-native'

import { AppText, Button, IconButton, ProgressBar } from '@/components/ui'
import { PermissionDeniedError, pickDocument, pickImageFromLibrary, takePhoto, type PickedFile } from '@/features/files/pickers'
import type { UploadItem } from '@/features/files/useProjectUploads'
import { useT } from '@/i18n'
import type { ProjectFile } from '@/services/api/types'
import { iconSize, radius, space, useTheme } from '@/theme'
import { formatRelativeTime } from '@/utils/dates'
import { formatBytes } from '@/utils/format'

export function UploadSourceBar({ onPicked, disabled = false }: { onPicked: (file: PickedFile) => void; disabled?: boolean }) {
  const t = useT()
  const [error, setError] = useState<string | null>(null)

  async function pick(source: () => Promise<PickedFile | null>) {
    setError(null)
    try {
      const file = await source()
      if (file) onPicked(file)
    } catch (cause) {
      setError(cause instanceof PermissionDeniedError ? cause.message : t('upload.pickerFailed'))
    }
  }

  return (
    <View style={styles.sourceBar}>
      <View style={styles.sources}>
        <Button variant="secondary" icon="camera-outline" label={t('upload.camera')} disabled={disabled} onPress={() => pick(takePhoto)} />
        <Button variant="secondary" icon="images-outline" label={t('upload.gallery')} disabled={disabled} onPress={() => pick(pickImageFromLibrary)} />
        <Button variant="secondary" icon="document-outline" label={t('upload.document')} disabled={disabled} onPress={() => pick(pickDocument)} />
      </View>
      {error ? (
        <AppText variant="caption" tone="error" accessibilityLiveRegion="polite">
          {error}
        </AppText>
      ) : null}
    </View>
  )
}

function FileThumb({ previewUri, mimeType }: { previewUri: string | null; mimeType: string }) {
  const { colors } = useTheme()
  if (previewUri) {
    return <Image source={{ uri: previewUri }} style={styles.thumb} contentFit="cover" accessible={false} />
  }
  const icon = mimeType.startsWith('image/') ? 'image-outline' : mimeType.includes('pdf') ? 'document-text-outline' : 'document-outline'
  return (
    <View style={[styles.thumb, styles.thumbIcon, { backgroundColor: colors.surface3 }]}>
      <Ionicons name={icon} size={iconSize.md} color={colors.textSecondary} />
    </View>
  )
}

export function UploadList({
  items,
  onCancel,
  onRetry,
  onDismiss,
}: {
  items: UploadItem[]
  onCancel: (id: string) => void
  onRetry: (id: string) => void
  onDismiss: (id: string) => void
}) {
  const t = useT()
  if (items.length === 0) return null
  return (
    <View style={styles.list}>
      {items.map((item) => (
        <View key={item.id} style={styles.item}>
          <FileThumb previewUri={item.file.previewUri} mimeType={item.file.mimeType} />
          <View style={styles.flex}>
            <AppText variant="bodyStrong" numberOfLines={1}>
              {item.file.name}
            </AppText>
            {item.status === 'uploading' ? (
              <View style={styles.progress}>
                <ProgressBar value={item.progress * 100} label={t('upload.uploadingA11y', { name: item.file.name })} />
                <AppText variant="caption" tone="muted">
                  {t('upload.progress', { percent: Math.round(item.progress * 100) })}
                </AppText>
              </View>
            ) : (
              <AppText variant="caption" tone="error" numberOfLines={2}>
                {item.error}
              </AppText>
            )}
          </View>
          {item.status === 'uploading' ? (
            <IconButton icon="close" label={t('upload.cancelA11y', { name: item.file.name })} onPress={() => onCancel(item.id)} />
          ) : (
            <>
              <IconButton icon="refresh" label={t('upload.retryA11y', { name: item.file.name })} tone="accent" onPress={() => onRetry(item.id)} />
              <IconButton icon="close" label={t('upload.dismissA11y', { name: item.file.name })} onPress={() => onDismiss(item.id)} />
            </>
          )}
        </View>
      ))}
    </View>
  )
}

export function FileRow({
  file,
  projectName,
  onOpen,
  onDelete,
}: {
  file: ProjectFile
  projectName?: string
  onOpen?: () => void
  onDelete?: () => void
}) {
  const t = useT()
  const { colors } = useTheme()
  const details = [formatBytes(file.size_bytes), formatRelativeTime(file.created_at), projectName ? t('files.inProject', { name: projectName }) : null]
    .filter(Boolean)
    .join(' · ')
  return (
    <Pressable
      accessibilityRole={onOpen ? 'button' : undefined}
      accessibilityHint={onOpen ? t('files.openHint') : undefined}
      disabled={!onOpen}
      onPress={onOpen}
      style={({ pressed }) => [styles.item, { backgroundColor: pressed ? colors.surface2 : 'transparent' }]}
    >
      <FileThumb previewUri={null} mimeType={file.mime_type} />
      <View style={styles.flex}>
        <AppText variant="bodyStrong" numberOfLines={1}>
          {file.name}
        </AppText>
        <AppText variant="caption" tone="muted" numberOfLines={1}>
          {details}
        </AppText>
      </View>
      {onDelete ? <IconButton icon="trash-outline" label={t('files.deleteA11y', { name: file.name })} tone="danger" onPress={onDelete} /> : null}
    </Pressable>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1, gap: 2 },
  sourceBar: { gap: space.sm },
  sources: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm },
  list: { gap: space.xs },
  item: { flexDirection: 'row', alignItems: 'center', gap: space.md, paddingHorizontal: space.lg, paddingVertical: space.sm, minHeight: 60 },
  thumb: { width: 44, height: 44, borderRadius: radius.sm },
  thumbIcon: { alignItems: 'center', justifyContent: 'center' },
  progress: { gap: 4, paddingTop: 4 },
})
