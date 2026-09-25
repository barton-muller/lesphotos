import { existsSync } from 'node:fs'
import { defineConfig, devices } from '@playwright/test'

// This suite runs against a fully isolated instance (own data dir, own
// account, own port) that tests/setup/test-server.sh bootstraps - never
// the developer's own :8001/:5173 dev instances (see CLAUDE.md's "Dev
// server ports": those are long-running and reused across sessions, not
// something a test run should touch or restart).
const PORT = process.env.LESPHOTOS_TEST_PORT ?? '8199'
const BASE_URL = `http://127.0.0.1:${PORT}`

// Some sandboxed dev containers pre-install only the full Chromium build
// at a fixed path (not the separate chrome-headless-shell Playwright's
// default headless mode wants), with no network access to fetch the
// missing piece - `npx playwright install` fails there with no useful
// recourse. Detected by path, not by an env var some container may or may
// not set: if this file exists, use it explicitly; otherwise fall back to
// Playwright's normal resolution (a real laptop/CI checkout that ran
// `npx playwright install` needs no override at all).
const SANDBOX_CHROMIUM = '/opt/pw-browsers/chromium'
const launchOptions = existsSync(SANDBOX_CHROMIUM) ? { executablePath: SANDBOX_CHROMIUM } : {}

export default defineConfig({
  testDir: './tests',
  // Each spec drives its own login/state rather than sharing a browser
  // across tests, matching CLAUDE.md's "test from a cold start" rule - a
  // warm session hides exactly the bugs a cold one catches.
  fullyParallel: true,
  // But NOT across workers: every project shares one backend and one
  // library (tests/setup/test-server.sh bootstraps a single account), so a
  // test that mutates server state (favorite-no-refresh) running at the
  // same time as another project's copy of itself both target ".first()
  // Favorite button" against the same live data - a real race, seen as a
  // flaky scrollTop mismatch that had nothing to do with the app. One
  // worker trades parallelism for correctness on a suite this small (~15s
  // total); a bigger suite would want per-test isolated libraries instead.
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  // A thumbnail tile only becomes a real <img> once its blob resolves via
  // IndexedDB/network (useThumbnail) - the favorite/select buttons render
  // immediately as placeholders, so waiting on those alone (as login()
  // does) isn't enough to prove a photo actually rendered. 10s default,
  // generous for a ~500-photo fake library over localhost.
  expect: { timeout: 10_000 },
  projects: [
    {
      name: 'desktop-chromium',
      use: { ...devices['Desktop Chrome'], launchOptions },
    },
    {
      // Android-shaped viewport + real touch input - the pinch/density and
      // narrow-layout checks need hasTouch, not just a narrow viewport;
      // see tests/pinch-zoom.spec.ts.
      name: 'mobile-android',
      use: { ...devices['Pixel 7'], launchOptions },
    },
  ],
  webServer: {
    command: './tests/setup/test-server.sh',
    url: `${BASE_URL}/lesphotos-api/health`,
    // The bootstrap script is idempotent (skips regenerating the library/
    // account/build if already present), so reusing an already-running
    // instance across repeated `npx playwright test` calls during local
    // iteration is safe and much faster than a full rebuild+reindex each
    // time.
    reuseExistingServer: true,
    timeout: 120_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
})
