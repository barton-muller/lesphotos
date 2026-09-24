import { test as base, expect } from '@playwright/test'

// The one account tests/setup/test-server.sh creates. Not a secret worth
// protecting - it only ever exists against the isolated test data dir in
// tests/.data, never a real library.
export const E2E_USERNAME = 'e2e'
export const E2E_PASSWORD = 'e2e-test-password-123'

// Logs in and waits for the grid to actually render - not just for
// navigation, since the interesting bugs in this app tend to live in what
// happens *after* the page loads (sync, IndexedDB, the virtualizer). See
// CLAUDE.md: "test from a cold start" - each test gets its own fresh
// context via Playwright's default per-test isolation, never a reused,
// already-logged-in page.
export async function login(page: import('@playwright/test').Page) {
  await page.goto('/')
  await page.fill('input[placeholder=username]', E2E_USERNAME)
  await page.fill('input[placeholder=password]', E2E_PASSWORD)
  await page.click('button[type=submit]')
  await expect(page.locator('[data-testid=photo-tile]').first()).toBeVisible({ timeout: 30_000 })

  // The grid rendering only means the FIRST sync page landed - metadata
  // sync (App.tsx, "on login and on regaining focus") keeps writing more
  // IndexedDB pages after that, each one bumping Timeline's tableVersion
  // and re-triggering its load effect. A test that starts interacting
  // immediately can race that tail of the sync: a scroll-position check
  // failed intermittently for exactly this reason (scrollTop drifting
  // during a still-in-flight sync page write, not from the interaction
  // being tested) until this wait was added. StatusBar's own
  // syncStatus==='syncing' is the real signal to wait on, not a fixed
  // sleep - see StatusBar.tsx.
  await expect(page.locator('[data-testid=status-bar]')).not.toContainText('Loading metadata', {
    timeout: 30_000,
  })
}

export const test = base
export { expect }
