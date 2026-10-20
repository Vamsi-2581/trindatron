// Explicit so every Jest preset (including jest-expo/node for integration tests) transpiles TypeScript.
module.exports = function (api) {
  api.cache(true)
  return { presets: ['babel-preset-expo'] }
}
