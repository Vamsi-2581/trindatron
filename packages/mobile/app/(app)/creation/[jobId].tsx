import { Image } from 'expo-image'
import { useLocalSearchParams } from 'expo-router'
import { useVideoPlayer, VideoView } from 'expo-video'
import { useEffect, useState } from 'react'
import { StyleSheet, View } from 'react-native'

import { MediaActions } from '@/components/media/MediaActions'
import { EmptyState, ErrorState, LoadingState, Notice, Screen } from '@/components/ui'
import { creationViewSource } from '@/features/media/creationFile'
import { creationFromJob } from '@/features/media/media'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useT } from '@/i18n'
import { getMediaJob } from '@/services/api/media'
import { space } from '@/theme'

type ViewSource = { uri: string; headers?: Record<string, string> }

/** One creation, full size: the image, or the video with the system's player controls. */
export default function CreationViewerScreen() {
  const t = useT()
  const { jobId } = useLocalSearchParams<{ jobId: string }>()
  const resource = useAsyncResource((signal) => getMediaJob(String(jobId), signal), [jobId])
  const job = resource.data
  const [source, setSource] = useState<ViewSource | null>(null)
  const [sourceFailed, setSourceFailed] = useState(false)

  useEffect(() => {
    if (!job) return
    const media = creationFromJob(job)
    if (media.status !== 'completed' || !media.contentPath) return
    let active = true
    let objectUrl: string | null = null
    creationViewSource(media)
      .then((next) => {
        if (next.uri.startsWith('blob:')) objectUrl = next.uri
        if (active) setSource(next)
      })
      .catch(() => active && setSourceFailed(true))
    return () => {
      active = false
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [job])

  if (resource.loading && !job) return <LoadingState />
  if (resource.error && !job) return <ErrorState message={resource.error.message} onRetry={resource.refresh} />
  if (!job) return null

  const media = creationFromJob(job)
  if (media.status !== 'completed') {
    return (
      <Screen>
        <EmptyState icon="hourglass-outline" title={t('media.notReady')} body={t(`media.status.${media.status}`)} />
      </Screen>
    )
  }

  return (
    <Screen scroll={false} edges={['left', 'right', 'bottom']}>
      <View style={styles.stage}>
        {source ? (
          media.kind === 'video' ? (
            <CreationVideo source={source} />
          ) : (
            <Image source={source} style={styles.media} contentFit="contain" transition={150} accessibilityLabel={t('media.kind.image')} />
          )
        ) : sourceFailed ? (
          <Notice tone="error">{t('media.actionFailed')}</Notice>
        ) : (
          <LoadingState />
        )}
      </View>
      <MediaActions media={media} showOpen={false} />
    </Screen>
  )
}

function CreationVideo({ source }: { source: ViewSource }) {
  const player = useVideoPlayer(source, (instance) => {
    instance.loop = false
  })
  return <VideoView player={player} style={styles.media} contentFit="contain" nativeControls fullscreenOptions={{ enable: true }} />
}

const styles = StyleSheet.create({
  stage: { flex: 1, minHeight: 280, justifyContent: 'center', marginBottom: space.md },
  media: { flex: 1, width: '100%' },
})
