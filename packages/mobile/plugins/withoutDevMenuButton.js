const { AndroidConfig, withAndroidManifest, withInfoPlist } = require('expo/config-plugins')

const KEY = 'EXDevMenuShowFloatingActionButton'

/**
 * The Expo development client draws a floating gear ("tools") button on top of
 * the app. It exists only in development builds — release builds contain no dev
 * menu at all — but it sits over the product UI, so it starts switched off. The
 * dev menu itself stays one shake (or a three-finger long press) away.
 */
module.exports = function withoutDevMenuButton(config) {
  const withAndroid = withAndroidManifest(config, (mod) => {
    const application = AndroidConfig.Manifest.getMainApplicationOrThrow(mod.modResults)
    AndroidConfig.Manifest.addMetaDataItemToMainApplication(application, KEY, 'false')
    return mod
  })
  return withInfoPlist(withAndroid, (mod) => {
    mod.modResults[KEY] = false
    return mod
  })
}
