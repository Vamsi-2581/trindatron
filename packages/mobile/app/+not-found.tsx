import { router } from 'expo-router'

import { Button, EmptyState, Screen } from '@/components/ui'
import { useT } from '@/i18n'

export default function NotFoundScreen() {
  const t = useT()
  return (
    <Screen edges={['top', 'bottom', 'left', 'right']}>
      <EmptyState
        icon="compass-outline"
        title={t('notFound.title')}
        body={t('notFound.body')}
        action={<Button label={t('notFound.home')} onPress={() => router.replace('/')} />}
      />
    </Screen>
  )
}
