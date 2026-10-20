import Ionicons from '@expo/vector-icons/Ionicons'
import { Image } from 'expo-image'
import { router } from 'expo-router'
import * as WebBrowser from 'expo-web-browser'
import { ActivityIndicator, Linking, Pressable, StyleSheet, View } from 'react-native'

import { AppText, Button, IconButton, ProgressBar } from '@/components/ui'
import type { MediaCreation } from '@/features/media/media'
import { useT } from '@/i18n'
import { authorizedSource } from '@/services/api/media'
import { radius, space, useTheme } from '@/theme'

import { MediaActions } from './MediaActions'

/** Progress, result and the actions the Core made possible for one creation. Nothing internal is shown. */
export function MediaResultCard({
  media,
  onDelete,
  onCreateAnother,
}: {
  media: MediaCreation
  onDelete?: () => void
  onCreateAnother?: () => void
}) {
  const { colors } = useTheme()
  const t = useT()
  const kind = t(`media.kind.${media.kind}`)
  // A creation streamed with signed links keeps using them; a Core job is read through its media paths.
  const signedLinks = Boolean(media.previewUrl || media.downloadUrl)
  const openUrl = media.previewUrl ?? media.downloadUrl
  const working = media.status === 'queued' || media.status === 'running'
  const viewable = media.status === 'completed' && Boolean(media.contentPath)
  const imageSource =
    media.status === 'completed' && media.kind === 'image'
      ? media.contentPath
        ? authorizedSource(media.contentPath)
        : media.previewUrl
          ? { uri: media.previewUrl }
          : null
      : null

  function openFull() {
    if (viewable) router.push({ pathname: '/creation/[jobId]', params: { jobId: media.id } })
    else if (media.previewUrl) void WebBrowser.openBrowserAsync(media.previewUrl)
  }

  return (
    <View style={[styles.card, { backgroundColor: colors.surface1, borderColor: colors.border }]} accessibilityLabel={`${kind}: ${t(`media.status.${media.status}`)}`}>
      {imageSource ? (
        <Pressable accessibilityRole="imagebutton" accessibilityLabel={t('media.open')} disabled={!viewable && !media.previewUrl} onPress={openFull}>
          <Image source={imageSource} style={styles.image} contentFit="contain" transition={150} accessibilityLabel={kind} />
        </Pressable>
      ) : (
        <Pressable
          accessibilityRole={viewable ? 'button' : undefined}
          accessibilityLabel={viewable ? t('media.open') : undefined}
          disabled={!viewable}
          onPress={openFull}
          style={[styles.placeholder, { backgroundColor: colors.surface2 }]}
        >
          {working ? (
            <ActivityIndicator color={colors.accent} />
          ) : (
            <Ionicons
              name={media.status === 'failed' ? 'alert-circle-outline' : media.kind === 'image' ? 'image-outline' : viewable ? 'play-circle-outline' : 'film-outline'}
              size={viewable && media.kind !== 'image' ? 44 : 28}
              color={media.status === 'failed' ? colors.error : colors.textSecondary}
            />
          )}
        </Pressable>
      )}

      <View style={styles.body}>
        <View style={styles.row}>
          <AppText variant="caption" tone={media.status === 'failed' ? 'error' : 'secondary'} style={styles.flex}>
            {media.durationSeconds && media.status === 'completed'
              ? `${kind} · ${t('media.seconds', { count: Math.round(media.durationSeconds) })}`
              : `${kind} · ${t(`media.status.${media.status}`)}`}
          </AppText>
          {onDelete && !working ? <IconButton icon="trash-outline" size="md" tone="danger" label={t('media.delete')} onPress={onDelete} /> : null}
        </View>
        {media.status === 'failed' ? (
          <AppText variant="caption" tone="muted">
            {t('media.failedNote')}
          </AppText>
        ) : null}
        {working && media.progress !== null ? <ProgressBar value={media.progress * 100} label={t('media.status.running')} /> : null}
        {signedLinks ? (
          media.status === 'completed' ? (
            <View style={styles.actions}>
              {openUrl ? <Button label={t('media.open')} variant="secondary" icon="open-outline" onPress={() => void WebBrowser.openBrowserAsync(openUrl)} /> : null}
              {media.downloadUrl ? (
                <Button label={t('media.download')} variant="ghost" icon="download-outline" onPress={() => void Linking.openURL(media.downloadUrl!)} />
              ) : null}
            </View>
          ) : null
        ) : (
          <MediaActions media={media} onCreateAnother={onCreateAnother} />
        )}
      </View>
    </View>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  card: { borderWidth: StyleSheet.hairlineWidth, borderRadius: radius.lg, overflow: 'hidden', width: '100%', maxWidth: 520, alignSelf: 'center' },
  image: { width: '100%', aspectRatio: 1 },
  placeholder: { width: '100%', aspectRatio: 16 / 9, alignItems: 'center', justifyContent: 'center' },
  body: { padding: space.md, gap: space.sm },
  row: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  actions: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm },
})
