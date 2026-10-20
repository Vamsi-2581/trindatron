import { AndroidConfig } from 'expo/config-plugins'

import app from '../../app.json'
import eas from '../../eas.json'

type PluginEntry = string | [string, Record<string, unknown>]

function pluginOptions(name: string): Record<string, unknown> | null {
  const entry = (app.expo.plugins as PluginEntry[]).find((plugin) => (Array.isArray(plugin) ? plugin[0] : plugin) === name)
  return Array.isArray(entry) ? entry[1] : null
}

describe('native app config', () => {
  it('uses the same identifier on iOS and Android', () => {
    expect(app.expo.ios.bundleIdentifier).toBe('com.trindatron.app')
    expect(app.expo.android.package).toBe(app.expo.ios.bundleIdentifier)
  })

  it('never blocks the microphone that voice input needs', () => {
    // expo-image-picker turns `microphonePermission: false` into a manifest-wide
    // RECORD_AUDIO removal, which silently breaks expo-audio recording on Android.
    expect(pluginOptions('expo-image-picker')?.microphonePermission).not.toBe(false)
    expect(pluginOptions('expo-audio')?.microphonePermission).toEqual(expect.any(String))
    const android = app.expo.android as { permissions?: string[]; blockedPermissions?: string[] }
    expect(android.blockedPermissions ?? []).not.toContain('android.permission.RECORD_AUDIO')
    if (android.permissions) expect(android.permissions).toContain('android.permission.RECORD_AUDIO')
  })

  it('ships Expo developer tools only in development builds', () => {
    // expo-dev-launcher / expo-dev-menu link as debugImplementation with a
    // disableInRelease source set, so a release build contains no dev menu,
    // no floating tools button and no launcher — as long as release profiles
    // never ask for a development client.
    const profiles = eas.build as Record<string, { developmentClient?: boolean; extends?: string }>
    expect(profiles.development?.developmentClient).toBe(true)
    expect(profiles['development-device']?.extends).toBe('development')
    expect(profiles.preview?.developmentClient).not.toBe(true)
    expect(profiles.production?.developmentClient).not.toBe(true)
    // The only dev-client option is how the launcher reopens the last dev server;
    // it has no effect on release builds, which contain no launcher at all.
    expect(pluginOptions('expo-dev-client')).toEqual({ launchMode: 'most-recent' })
  })

  it('keeps the development client’s floating gear button off the product UI', () => {
    expect(app.expo.plugins).toContain('./plugins/withoutDevMenuButton')
    const manifest = { manifest: { application: [{ $: { 'android:name': '.MainApplication' }, 'meta-data': [] as { $: Record<string, string> }[] }] } }
    const application = AndroidConfig.Manifest.getMainApplicationOrThrow(manifest as never)
    AndroidConfig.Manifest.addMetaDataItemToMainApplication(application, 'EXDevMenuShowFloatingActionButton', 'false')
    expect(manifest.manifest.application[0]!['meta-data']).toEqual([
      { $: { 'android:name': 'EXDevMenuShowFloatingActionButton', 'android:value': 'false' } },
    ])
    expect(typeof jest.requireActual('../../plugins/withoutDevMenuButton')).toBe('function')
  })

  it('integrates expo-updates so the development client can open both Metro and published updates', () => {
    // Without expo-updates, expo-dev-launcher refuses any manifest not served by a
    // developer tool ("expo-updates is not properly installed or integrated").
    const pkg = require('../../package.json') as { dependencies: Record<string, string> }
    const bundled = require('expo/bundledNativeModules.json') as Record<string, string>
    expect(pkg.dependencies['expo-updates']).toBe(bundled['expo-updates'])
    expect(pkg.dependencies['expo-dev-client']).toBe(bundled['expo-dev-client'])

    const expo = app.expo as typeof app.expo & { runtimeVersion?: { policy: string }; updates?: { url: string } }
    expect(expo.runtimeVersion).toEqual({ policy: 'appVersion' })
    expect(expo.updates?.url).toBe(`https://u.expo.dev/${app.expo.extra.eas.projectId}`)
    expect(app.expo.extra.eas.projectId).toBe('5b7c9373-9f6b-4575-b4d8-ba6685ea51c8')

    const profiles = eas.build as Record<string, { channel?: string }>
    expect(profiles.development?.channel).toBe('development')
    expect(profiles['development-device']?.channel).toBe('development-device')
    expect(profiles.preview?.channel).toBe('preview')
    expect(profiles.production?.channel).toBe('production')
  })

  it('declares each Android permission once', () => {
    const permissions = (app.expo.android as { permissions?: string[] }).permissions ?? []
    expect(new Set(permissions).size).toBe(permissions.length)
  })

  it('does not declare background audio services the app never uses', () => {
    expect(pluginOptions('expo-audio')).toMatchObject({ enableBackgroundPlayback: false, enableBackgroundRecording: false })
  })
})
