/** Unit tests. The backend is mocked here; see jest.integration.config.js for the real Core. */
const transformIgnorePatterns = [
  'node_modules/(?!((jest-)?react-native|@react-native(-community)?)|expo(nent)?|@expo(nent)?/.*|@expo-google-fonts/.*|react-navigation|@react-navigation/.*|standard-navigation|@sentry/react-native|native-base|react-native-svg|marked|@kesha-antonov/.*)',
]

module.exports = {
  preset: 'jest-expo',
  setupFiles: ['<rootDir>/jest.setup.ts'],
  moduleNameMapper: {
    '^@/(.*)$': '<rootDir>/src/$1',
    // expo-router/testing-library re-mocks Reanimated with its own `mock`, which cannot load under Jest (see
    // jest/reanimatedMock.js). Pointing that path at ours keeps one Reanimated in every test, router-rendered or not.
    '^react-native-reanimated/mock$': '<rootDir>/jest/reanimatedMock.js',
  },
  testPathIgnorePatterns: ['/node_modules/', '\\.integration\\.test\\.ts$', '<rootDir>/dist/'],
  transformIgnorePatterns,
  testTimeout: 15000,
}
