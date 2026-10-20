/**
 * Runs the API layer against a REAL TRINITY CORE (no mocks):
 *   CORE_INTEGRATION_URL=http://127.0.0.1:8000 npm run test:integration
 * Skips itself when the variable is absent. See docs/MOBILE.md, "Testes".
 */
const unit = require('./jest.config.js')

module.exports = {
  preset: 'jest-expo/node',
  testMatch: ['**/*.integration.test.ts'],
  moduleNameMapper: {
    '^expo/fetch$': '<rootDir>/src/__integration__/nodeFetch.ts',
    ...unit.moduleNameMapper,
  },
  transformIgnorePatterns: unit.transformIgnorePatterns,
  testTimeout: 120000,
}
