import { Alert, Platform } from 'react-native'

import { t } from '@/i18n'

/** Cross-platform confirmation. react-native-web's Alert ignores buttons, so Web uses confirm(). */
export function confirmAction(options: {
  title: string
  message: string
  confirmLabel: string
  destructive?: boolean
}): Promise<boolean> {
  if (Platform.OS === 'web') {
    return Promise.resolve(typeof window !== 'undefined' && window.confirm(`${options.title}\n\n${options.message}`))
  }
  return new Promise((resolve) => {
    Alert.alert(
      options.title,
      options.message,
      [
        { text: t('common.cancel'), style: 'cancel', onPress: () => resolve(false) },
        { text: options.confirmLabel, style: options.destructive ? 'destructive' : 'default', onPress: () => resolve(true) },
      ],
      { cancelable: true, onDismiss: () => resolve(false) },
    )
  })
}
