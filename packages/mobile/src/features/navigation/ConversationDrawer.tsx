import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { BackHandler, StyleSheet, useWindowDimensions, View } from 'react-native'
import { Drawer } from 'react-native-drawer-layout'
import { KeyboardController } from 'react-native-keyboard-controller'
import { useSafeAreaInsets } from 'react-native-safe-area-context'

import { useT } from '@/i18n'
import { useTheme } from '@/theme'

import { ConversationsPanel } from './ConversationsPanel'

/** From this width the conversations stay docked beside the content (tablet landscape, desktop browsers). */
export const DOCKED_PANEL_BREAKPOINT = 1024
const DOCKED_WIDTH = 300
const DRAWER_MAX_WIDTH = 320

interface DrawerApi {
  open: () => void
  close: () => void
  /** The panel is permanently visible, so there is nothing to open. */
  docked: boolean
  /** Lets a screen whose left edge is free (Home) accept the edge swipe. */
  setEdgeSwipe: (enabled: boolean) => void
}

const DrawerContext = createContext<DrawerApi>({ open: () => undefined, close: () => undefined, docked: false, setEdgeSwipe: () => undefined })

export function useConversationDrawer(): DrawerApi {
  return useContext(DrawerContext)
}

/**
 * The side menu, on react-native-drawer-layout (MIT) — the drawer React Navigation itself uses. Its open, close and
 * edge swipe run on Reanimated and Gesture Handler, off the JavaScript thread, instead of the hand-written
 * PanResponder this replaces. From DOCKED_PANEL_BREAKPOINT the same component lays the panel out permanently beside
 * the content.
 *
 * The panel mounts the first time it is opened (or docked): a Home that nobody navigates away from never asks the
 * Core for the conversation list.
 */
export function ConversationDrawerProvider({ children }: { children: ReactNode }) {
  const { colors } = useTheme()
  const t = useT()
  const insets = useSafeAreaInsets()
  const { width } = useWindowDimensions()
  const docked = width >= DOCKED_PANEL_BREAKPOINT
  const [isOpen, setOpen] = useState(false)
  const [everOpened, setEverOpened] = useState(false)
  const [edgeSwipe, setEdgeSwipe] = useState(false)

  const open = useCallback(() => {
    if (docked) return
    setEverOpened(true)
    setOpen(true)
  }, [docked])
  const close = useCallback(() => {
    setOpen(false)
    // The search field lives in the panel: typed into and left focused, its keyboard stayed up after the menu
    // closed — over Home's composer, for an input no longer on screen. The drawer itself closes the keyboard only on
    // a drag; the overlay, back and navigating away all come through here.
    if (KeyboardController.isVisible()) void KeyboardController.dismiss()
  }, [])

  useEffect(() => {
    if (!isOpen || docked) return
    const subscription = BackHandler.addEventListener('hardwareBackPress', () => {
      close()
      return true
    })
    return () => subscription.remove()
  }, [isOpen, docked, close])

  const api = useMemo(() => ({ open, close, docked, setEdgeSwipe }), [open, close, docked])

  return (
    <DrawerContext.Provider value={api}>
      <Drawer
        open={docked || isOpen}
        onOpen={open}
        onClose={close}
        drawerType={docked ? 'permanent' : 'front'}
        swipeEnabled={!docked && (edgeSwipe || isOpen)}
        overlayAccessibilityLabel={t('drawer.close')}
        overlayStyle={{ backgroundColor: colors.overlay }}
        drawerStyle={[
          styles.drawer,
          {
            width: docked ? DOCKED_WIDTH : Math.min(DRAWER_MAX_WIDTH, Math.round(width * 0.86)),
            backgroundColor: colors.surface1,
            borderRightColor: colors.separator,
            paddingTop: insets.top,
            paddingBottom: insets.bottom,
          },
        ]}
        renderDrawerContent={() =>
          docked || everOpened ? <ConversationsPanel onNavigate={close} onClose={docked ? undefined : close} /> : <View />
        }
      >
        <View style={[styles.flex, { backgroundColor: colors.bg }]}>{children}</View>
      </Drawer>
    </DrawerContext.Provider>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1 },
  drawer: { borderRightWidth: StyleSheet.hairlineWidth },
})
