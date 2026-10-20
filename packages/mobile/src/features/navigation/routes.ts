import type { Href } from 'expo-router'

/**
 * Routes whose generated type is not what the link says.
 *
 * expo-router writes .expo/types/router.d.ts from Metro, so a screen added
 * without starting the bundler is not in that union yet — and a nested
 * index route is listed under its file path ("/support/index"), never as the
 * path people actually navigate to. Rather than casting at every call site,
 * each such route is named once here.
 */
export const SUPPORT_HREF = '/support' as Href

/** Plans and their prices, from the Core's catalog. */
export const PLANS_HREF = '/plans' as Href

/** Billing: the subscription, how it is paid, and the payment history. */
export const BILLING_HREF = '/billing' as Href

/** How much of the plan has been used. */
export const USAGE_HREF = '/usage' as Href

/** Agendados: instructions TRINDATRON carries out on a clock. */
export const SCHEDULED_HREF = '/scheduled' as Href

/** Computer Use: say what you want done, the Core does it in an isolated environment. */
export const COMPUTER_USE_HREF = '/computer-use' as Href

/** Settings and the screens under it. */
export const SETTINGS_HREF = '/settings' as Href
export const SETTINGS_DEVICES_HREF = '/settings/devices' as Href
export const SETTINGS_MEMORIES_HREF = '/settings/memories' as Href
export const SETTINGS_PRIVACY_HREF = '/settings/privacy' as Href
