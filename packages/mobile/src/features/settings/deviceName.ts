import type { IconName } from '@/components/ui'

export type DevicePlatform = 'android' | 'ios' | 'windows' | 'mac' | 'linux' | 'unknown'

export interface DeviceDescription {
  /** `app` for TRINDATRON's own app, `browser` for the Web app in a browser, `unknown` when nothing is recognisable. */
  kind: 'app' | 'browser' | 'unknown'
  /** The browser's product name ("Chrome"), for `browser` only. */
  browser: string | null
  platform: DevicePlatform
  icon: IconName
}

const BROWSERS: [RegExp, string][] = [
  // Order matters: Edge and Opera also say "Chrome", and every Chromium browser also says "Safari".
  [/\bEdg(?:e|A|iOS)?\//, 'Edge'],
  [/\b(?:OPR|Opera)\//, 'Opera'],
  [/\bSamsungBrowser\//, 'Samsung Internet'],
  [/\bFirefox\/|\bFxiOS\//, 'Firefox'],
  [/\bChrome\/|\bCriOS\//, 'Chrome'],
  [/\bVersion\/[\d.]+.*\bSafari\//, 'Safari'],
]

function platformOf(userAgent: string): DevicePlatform {
  if (/Android/i.test(userAgent) || /\bokhttp\//i.test(userAgent)) return 'android'
  if (/iPhone|iPad|iPod|\bCFNetwork\/|\bDarwin\//i.test(userAgent)) return 'ios'
  if (/Windows NT/i.test(userAgent)) return 'windows'
  if (/Macintosh|Mac OS X/i.test(userAgent)) return 'mac'
  if (/Linux|X11/i.test(userAgent)) return 'linux'
  return 'unknown'
}

const ICONS: Record<DevicePlatform, IconName> = {
  android: 'logo-android',
  ios: 'logo-apple',
  windows: 'desktop-outline',
  mac: 'laptop-outline',
  linux: 'desktop-outline',
  unknown: 'hardware-chip-outline',
}

/**
 * What a signed-in session was, read from the user agent the Core recorded: never a guess beyond what the string
 * says, and no version numbers, which help nobody decide whether to end a session.
 *
 * The app's HTTP client does not name the product (React Native's is okhttp on Android, CFNetwork on iOS), so a
 * session carrying one of those is TRINDATRON's own app; anything with a browser token is the Web app.
 */
export function describeDevice(userAgent: string | null | undefined): DeviceDescription {
  const ua = userAgent?.trim() ?? ''
  if (!ua) return { kind: 'unknown', browser: null, platform: 'unknown', icon: ICONS.unknown }
  const platform = platformOf(ua)
  const browser = BROWSERS.find(([pattern]) => pattern.test(ua))?.[1] ?? null
  if (browser && /Mozilla\//.test(ua)) return { kind: 'browser', browser, platform, icon: platform === 'android' || platform === 'ios' ? 'phone-portrait-outline' : ICONS[platform] }
  if (/\bokhttp\/|\bCFNetwork\/|\bExpo\b|\bExponent\//i.test(ua)) return { kind: 'app', browser: null, platform, icon: ICONS[platform] }
  return { kind: 'unknown', browser: null, platform, icon: ICONS[platform] }
}
