import { router, type Href } from 'expo-router'
import { Pressable, StyleSheet } from 'react-native'

import { AppText } from './AppText'

export function TextLink({ label, href, onPress }: { label: string; href?: Href; onPress?: () => void }) {
  return (
    <Pressable
      accessibilityRole="link"
      hitSlop={8}
      onPress={() => {
        if (onPress) onPress()
        else if (href) router.push(href)
      }}
      style={({ pressed }) => [styles.link, pressed && styles.pressed]}
    >
      <AppText variant="caption" tone="accent" style={styles.text}>
        {label}
      </AppText>
    </Pressable>
  )
}

const styles = StyleSheet.create({
  link: { minHeight: 32, justifyContent: 'center' },
  pressed: { opacity: 0.6 },
  text: { fontWeight: '600' },
})
