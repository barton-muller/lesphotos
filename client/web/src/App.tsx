import { useEffect, useRef, useState } from 'react'
import { useLiveQuery } from 'dexie-react-hooks'
import { Timeline } from './Timeline'
import { StarIcon } from './StarIcon'
import { StatusBar } from './StatusBar'
import { useIsNarrow } from './useIsNarrow'
import { checkSession, login, logout } from './api'
import { db } from './db'
import { downloadAllThumbnails, forceFullSync, isLocalStorageError, syncTimeline } from './sync'

// Single-column shell: a thin top bar, the grid, and a status bar. The left
// panel is gone entirely - on a phone it cost more than half the width, and
// everything it held either moved to the top bar (starred filter, account) or
// stopped being a control at all (grid density is pinch-to-zoom now, and sync
// runs by itself).
// Records that this device completed a login, so an unreachable server can
// be told apart from a genuinely logged-out browser. The auth cookie is
// httponly by design, so the client cannot look at it - without this marker
// there is nothing to distinguish "the session check couldn't run" from
// "first visit". Deliberately not a credential: it grants nothing, every
// request is still authorised by the cookie server-side, and the worst case
// is a stale true, which the next reachable /session corrects.
const SESSION_MARKER = 'lesphotos-had-session'

function hadSession(): boolean {
  try {
    return localStorage.getItem(SESSION_MARKER) === '1'
  } catch {
    return false
  }
}

function rememberSession(had: boolean) {
  try {
    if (had) localStorage.setItem(SESSION_MARKER, '1')
    else localStorage.removeItem(SESSION_MARKER)
  } catch {
    // Private-mode storage failures just cost the offline-restore nicety.
  }
}

function App() {
  const [syncStatus, setSyncStatus] = useState<'idle' | 'syncing' | 'done' | 'error'>('idle')
  const [syncMessage, setSyncMessage] = useState('')
  const [loginStatus, setLoginStatus] = useState('')
  const [loggedIn, setLoggedIn] = useState(false)
  // Set when the session check couldn't reach the server and we kept the
  // user in on the strength of the marker above - the grid is served from
  // IndexedDB, so it still works; the status bar says why it's not syncing.
  const [serverUnreachable, setServerUnreachable] = useState('')
  // Set when a sync failed locally (an IndexedDB write, not a network
  // call) - distinct from serverUnreachable, since the server answered
  // fine and "Offline" would send the user looking at the wrong thing.
  const [localStorageError, setLocalStorageError] = useState('')
  // Starts true, not false: the login-vs-app decision depends on an async
  // session check (below) - rendering the login form immediately would
  // flash it for a moment even when a valid auth-token cookie already
  // exists (a real UX regression, not just a cosmetic flicker, for the
  // exact case this feature exists to fix).
  const [checkingSession, setCheckingSession] = useState(true)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [starredOnly, setStarredOnly] = useState(false)
  // Adjustable grid density (3-20 photos/row) - per-viewer preference, kept
  // in localStorage rather than synced server-side, matching the pattern
  // for lightweight per-device UI conveniences.
  const [columns, setColumns] = useState(() => {
    const stored = Number(localStorage.getItem('lesphotos-columns'))
    return stored >= 3 && stored <= 20 ? stored : 6
  })

  function handleColumnsChange(next: number) {
    setColumns(next)
    localStorage.setItem('lesphotos-columns', String(next))
  }

  // Guards against overlapping syncs. Four things trigger one - login,
  // the tab becoming visible, the manual button, and a rename needing
  // fresh rows - and a full sync takes tens of seconds, so overlap is
  // easy. Two concurrent runs each report their own done/total into the
  // same status line, which is why the counter appeared to jump around
  // and the total kept changing: each had computed its own toFetch at a
  // different moment. A ref rather than state: it has to be readable and
  // settable synchronously, before any re-render, or two calls in the
  // same tick would both get through.
  const syncing = useRef(false)
  // When the last sync finished, so a burst of visibility/focus events -
  // mobile Safari fires both for one real foreground transition, and
  // sometimes fires focus again on its own shortly after (address bar
  // showing/hiding, a click stealing and returning focus) - collapses into
  // one sync instead of a fresh one each time. syncing.current alone only
  // stops two calls overlapping; it does nothing once the first has
  // finished, which is what made "keeps re-triggering after it completes"
  // possible even though no two syncs ever ran concurrently.
  const lastSyncedAt = useRef(0)
  const MIN_RESYNC_INTERVAL_MS = 5000
  // Separate from syncStatus: downloading every thumbnail can run at the
  // same time nothing else is happening, and needs its own progress line
  // rather than fighting metadata sync for the same status text.
  const [downloadingThumbs, setDownloadingThumbs] = useState(false)
  const [thumbDownloadMessage, setThumbDownloadMessage] = useState('')

  // Metadata only, always - this is what makes login fast regardless of
  // library size (JSON, never image bytes) and what makes the grid appear
  // incrementally rather than after every page has landed. Thumbnail PIXELS
  // are runDownloadAll's job, triggered explicitly, never implicitly here.
  async function runSync(force = false) {
    if (syncing.current) return
    if (!force && Date.now() - lastSyncedAt.current < MIN_RESYNC_INTERVAL_MS) return
    syncing.current = true
    setSyncStatus('syncing')
    try {
      const result = await (force ? forceFullSync : syncTimeline)((done) =>
        setSyncMessage(`${done} loaded`),
      )
      setServerUnreachable('')
      setLocalStorageError('')
      setSyncStatus('done')
      setSyncMessage(
        `${result.added} new, ${result.updated} updated, ${result.removed} removed, ${result.total} total`,
      )
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      // A sync is also a live authorisation check, and the only place a
      // session can be found to have expired while the app is open. A 401
      // is the server explicitly rejecting the cookie, so drop the session
      // properly. A local IndexedDB write failure is a third case, distinct
      // from both: the server was reachable and answered fine, so it must
      // not be shown as "Offline" - only a genuine fetch/network failure
      // means the server is unreachable.
      if (/\(401\)/.test(message)) {
        rememberSession(false)
        setLoggedIn(false)
        setLoginStatus('Session expired - please log in again')
      } else if (isLocalStorageError(err)) {
        setLocalStorageError(message)
      } else {
        setServerUnreachable(message)
      }
      setSyncStatus('error')
      setSyncMessage(message)
    } finally {
      syncing.current = false
      lastSyncedAt.current = Date.now()
    }
  }

  // "Download all for offline": the one place thumbnail PIXELS are fetched
  // in bulk. Independent of syncing.current - metadata sync and a bulk
  // thumbnail download are different operations and either should be able
  // to run while the other is idle - but its own flag stops two downloads
  // overlapping the same way.
  const downloadingRef = useRef(false)
  const downloadAbortRef = useRef<AbortController | null>(null)
  async function runDownloadAll() {
    if (downloadingRef.current) return
    downloadingRef.current = true
    setDownloadingThumbs(true)
    setThumbDownloadMessage('')
    const controller = new AbortController()
    downloadAbortRef.current = controller
    try {
      const result = await downloadAllThumbnails(
        (done, total) => setThumbDownloadMessage(`${done}/${total} thumbnails`),
        controller.signal,
      )
      setThumbDownloadMessage(
        controller.signal.aborted
          ? `stopped - ${result.downloaded} downloaded`
          : result.failed
            ? `${result.downloaded} downloaded, ${result.failed} failed`
            : `${result.downloaded} downloaded`,
      )
    } catch (err) {
      setThumbDownloadMessage(err instanceof Error ? err.message : String(err))
    } finally {
      downloadAbortRef.current = null
      downloadingRef.current = false
      setDownloadingThumbs(false)
    }
  }

  function stopDownloadAll() {
    downloadAbortRef.current?.abort()
  }

  async function handleLogout() {
    await logout()
    // Drop the cached library too, not just the cookie: these are one
    // user's photos, and leaving them on the device for whoever logs in
    // next is both confusing and a privacy leak. Cheap to rebuild - the
    // next login syncs.
    try {
      await db.photos.clear()
      await db.fullres.clear()
      await db.thumbnails.clear()
    } catch {
      // A failed cache clear must not strand someone logged in.
    }
    rememberSession(false)
    setLoggedIn(false)
    setPassword('')
    setLoginStatus('')
  }

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault()
    setLoginStatus('Logging in…')
    try {
      await login(username, password)
      rememberSession(true)
      setLoginStatus('Logged in')
      setServerUnreachable('')
      setLoggedIn(true)
    } catch (err) {
      setLoginStatus(`Login failed: ${err instanceof Error ? err.message : String(err)}`)
    }
  }

  // Session restore: on mount, check whether the existing auth-token cookie
  // is still valid before falling back to the login form - without this,
  // every reload forced a fresh login even though the cookie itself was
  // still good for up to 30 days, which is the actual gap this closes.
  //
  // Only an explicit "no" from the server drops the session. If the check
  // could not be made at all - server stopped, NAS asleep, a 502 from the
  // proxy in front of it - someone who was logged in before stays logged
  // in: the alternative was showing them a login form that could only
  // answer "Login failed: login failed (502)", locking them out of a
  // cached library that works perfectly well offline.
  useEffect(() => {
    checkSession().then((state) => {
      if (state.status === 'authenticated') {
        rememberSession(true)
        setLoggedIn(true)
        setServerUnreachable('')
        if (state.username) setUsername(state.username)
      } else if (state.status === 'anonymous') {
        rememberSession(false)
        setLoggedIn(false)
      } else if (hadSession()) {
        setServerUnreachable(state.reason)
        setLoggedIn(true)
      }
      setCheckingSession(false)
    })
  }, [])

  // Sync-on-open: fires once login succeeds (fresh login or restored
  // session alike), so a user doesn't need to think about syncing
  // routinely - only an explicit "still see the manual refresh icon for
  // forcing a re-sync" affordance remains.
  useEffect(() => {
    if (loggedIn) runSync()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loggedIn])

  // Re-sync when the app comes back to the foreground, so photos added while
  // it was backgrounded show up without the user asking. Chosen over a timer:
  // it costs nothing while idle, which matters on a phone, and returning to
  // the app is exactly when a stale grid would be noticed.
  useEffect(() => {
    if (!loggedIn) return
    const onVisible = () => {
      if (document.visibilityState === 'visible') runSync()
    }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', onVisible)
    return () => {
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('focus', onVisible)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loggedIn])

  const photoCount = useLiveQuery(() => db.photos.count(), [], null)
  // A bulk download or a lazily-fetched tile both write to db.thumbnails
  // (its own table since db.ts's v9 schema), and this is what lets the
  // status bar count up in real time without either code path having to
  // know the bar exists. An indexed count here, not a filter over
  // db.photos - that used to deserialize every cached thumbnail blob just
  // to test its presence, on every single write to the photos table.
  const thumbnailCount = useLiveQuery(() => db.thumbnails.count(), [], null)
  // Still needed by Timeline: the scrubber rail collapses to a touch handle
  // on a phone even though the sidebar it used to share space with is gone.
  const narrow = useIsNarrow()

  const [menuOpen, setMenuOpen] = useState(false)
  useEffect(() => {
    if (!menuOpen) return
    const close = () => setMenuOpen(false)
    // Any click outside closes it; the button itself stops propagation.
    window.addEventListener('click', close)
    return () => window.removeEventListener('click', close)
  }, [menuOpen])

  if (checkingSession) {
    return (
      <div
        data-testid="session-check"
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100dvh',
          color: 'var(--text-muted)',
        }}
      >
        Checking session…
      </div>
    )
  }

  if (!loggedIn) {
    return (
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100dvh',
          padding: 24,
        }}
      >
        <form
          onSubmit={handleLogin}
          style={{ display: 'flex', flexDirection: 'column', gap: 10, width: 260 }}
        >
          <div style={{ fontWeight: 700, fontSize: 20, marginBottom: 4 }}>lesphotos</div>
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="username"
            autoCapitalize="none"
            autoCorrect="off"
            autoComplete="username"
            style={{ padding: 10, fontSize: 16 }}
          />
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="password"
            style={{ padding: 10, fontSize: 16 }}
          />
          <button type="submit" style={{ padding: 10 }}>
            Log in
          </button>
          <span data-testid="login-status" style={{ fontSize: 12, color: 'var(--text-muted)' }}>
            {loginStatus}
          </span>
        </form>
      </div>
    )
  }

  return (
    // 100dvh, not 100vh: on mobile the URL bar hides and shows, and vh is
    // fixed to the largest viewport - so the status bar sat below the fold.
    <div style={{ display: 'flex', flexDirection: 'column', height: '100dvh' }}>
      <header
        style={{
          flexShrink: 0,
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '6px 10px',
          borderBottom: '1px solid var(--border)',
          background: 'var(--surface)',
        }}
      >
        <button
          type="button"
          data-testid="starred-only-toggle"
          aria-pressed={starredOnly}
          title={starredOnly ? 'Showing starred only' : 'Show starred only'}
          onClick={() => setStarredOnly((v) => !v)}
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            width: 36,
            height: 36,
            padding: 0,
            borderRadius: 'var(--radius)',
            border: '1px solid var(--border)',
            // The fill carries the state, so the button itself only needs to
            // signal "active" - a filled gold star on a tinted background.
            background: starredOnly ? 'var(--surface-hover)' : 'var(--surface)',
            color: starredOnly ? 'var(--accent)' : 'var(--text-muted)',
            cursor: 'pointer',
          }}
        >
          <StarIcon filled={starredOnly} size={18} />
        </button>

        {/* Grid density (photos per row) previously had no discoverable
            trigger at all - pinch/ctrl+wheel over the grid works but isn't
            something anyone would find without being told. This is that
            control made visible; the gesture keeps working alongside it. */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            border: '1px solid var(--border)',
            borderRadius: 'var(--radius)',
            overflow: 'hidden',
          }}
        >
          <button
            type="button"
            data-testid="density-decrease"
            aria-label="Fewer photos per row"
            title="Fewer photos per row"
            onClick={() => handleColumnsChange(Math.max(3, columns - 1))}
            disabled={columns <= 3}
            style={{
              width: 30,
              height: 36,
              padding: 0,
              border: 'none',
              borderRight: '1px solid var(--border)',
              background: 'var(--surface)',
              color: columns <= 3 ? 'var(--text-muted)' : 'inherit',
              cursor: columns <= 3 ? 'default' : 'pointer',
              fontSize: 16,
            }}
          >
            −
          </button>
          <button
            type="button"
            data-testid="density-increase"
            aria-label="More photos per row"
            title="More photos per row"
            onClick={() => handleColumnsChange(Math.min(20, columns + 1))}
            disabled={columns >= 20}
            style={{
              width: 30,
              height: 36,
              padding: 0,
              border: 'none',
              background: 'var(--surface)',
              color: columns >= 20 ? 'var(--text-muted)' : 'inherit',
              cursor: columns >= 20 ? 'default' : 'pointer',
              fontSize: 16,
            }}
          >
            +
          </button>
        </div>

        {/* Timeline portals its Select/Merge controls in here, so they share
            the header row instead of costing a second one - its selection
            state stays where it is used rather than being lifted into App. */}
        <div
          id="lesphotos-toolbar-slot"
          style={{ flex: 1, minWidth: 0, display: 'flex', alignItems: 'center', gap: 8, overflowX: 'auto' }}
        />

        <div style={{ position: 'relative' }}>
          <button
            type="button"
            data-testid="profile-button"
            aria-label="Account"
            onClick={(e) => {
              e.stopPropagation()
              setMenuOpen((v) => !v)
            }}
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              width: 36,
              height: 36,
              padding: 0,
              borderRadius: '50%',
              border: '1px solid var(--border)',
              background: 'var(--surface)',
              cursor: 'pointer',
            }}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
                 stroke="currentColor" strokeWidth="2" strokeLinecap="round"
                 aria-hidden="true">
              <circle cx="12" cy="8" r="3.5" />
              <path d="M4.5 20a7.5 7.5 0 0 1 15 0" />
            </svg>
          </button>
          {menuOpen && (
            <div
              data-testid="profile-menu"
              onClick={(e) => e.stopPropagation()}
              style={{
                position: 'absolute',
                top: 42,
                right: 0,
                zIndex: 60,
                minWidth: 160,
                width: 'max-content',
                padding: 6,
                borderRadius: 'var(--radius)',
                border: '1px solid var(--border)',
                background: 'var(--surface)',
                boxShadow: '0 6px 24px rgba(0,0,0,0.18)',
              }}
            >
              <div style={{ padding: '6px 8px', fontSize: 12, color: 'var(--text-muted)' }}>
                {username ? `Signed in as ${username}` : 'Signed in'}
              </div>
              <button
                type="button"
                data-testid="sync-now"
                onClick={() => {
                  setMenuOpen(false)
                  runSync()
                }}
                style={{ display: 'block', width: '100%', textAlign: 'left', padding: '8px', whiteSpace: 'nowrap' }}
              >
                Sync now
              </button>
              <button
                type="button"
                data-testid="full-resync"
                title="Re-fetch and re-diff every photo's metadata against the server, ignoring the delta-sync watermark - use this if a server-side change (e.g. a direct DB fix) didn't show up via the normal sync"
                onClick={() => {
                  setMenuOpen(false)
                  runSync(true)
                }}
                style={{ display: 'block', width: '100%', textAlign: 'left', padding: '8px', whiteSpace: 'nowrap' }}
              >
                Full resync
              </button>
              <button
                type="button"
                data-testid="sync-all"
                title={
                  downloadingThumbs
                    ? 'Stop downloading - photos already fetched stay saved'
                    : 'Download every thumbnail so the whole library works offline'
                }
                onClick={() => {
                  setMenuOpen(false)
                  if (downloadingThumbs) {
                    stopDownloadAll()
                  } else {
                    runDownloadAll()
                  }
                }}
                style={{ display: 'block', width: '100%', textAlign: 'left', padding: '8px', whiteSpace: 'nowrap' }}
              >
                {downloadingThumbs ? 'Stop downloading' : 'Download Thumbnails'}
              </button>
              <button
                type="button"
                data-testid="log-out"
                onClick={() => {
                  setMenuOpen(false)
                  handleLogout()
                }}
                style={{ display: 'block', width: '100%', textAlign: 'left', padding: '8px', whiteSpace: 'nowrap' }}
              >
                Log out
              </button>
            </div>
          )}
        </div>
      </header>

      <div style={{ flex: 1, minWidth: 0, minHeight: 0 }}>
        <Timeline
          starredOnly={starredOnly}
          columns={columns}
          onColumnsChange={handleColumnsChange}
          onNeedsSync={runSync}
          narrow={narrow}
        />
      </div>

      <StatusBar
        syncStatus={syncStatus}
        syncMessage={syncMessage}
        photoCount={photoCount}
        thumbnailCount={thumbnailCount}
        downloadingThumbs={downloadingThumbs}
        thumbDownloadMessage={thumbDownloadMessage}
        offline={serverUnreachable !== ''}
        localStorageError={localStorageError}
      />
    </div>
  )
}

export default App
