import { defineConfig } from '@playwright/test'

// Transport unit tests stub browser globals and need neither a browser nor servers.
// The complete browser suite keeps its separate, unchanged repository config.
export default defineConfig({
  testDir: '.',
  testMatch: 'assistant-api.spec.ts',
  workers: 1,
  reporter: 'list',
  outputDir: '../test-results/unit',
})
