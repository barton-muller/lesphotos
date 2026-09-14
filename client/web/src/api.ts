// Client for server/api.py - the FastAPI service in front of appdb/reorganize.py.
// Favorite toggling and moment rename/merge MUST go through this, never
// PhotoView's favoriteMedia directly, per the hard constraint established
// in plan.md: appdb is the source of truth, PhotoView's own favorite flag
// is only a synced read-optimization.

const API_BASE = `${window.location.origin}/lesphotos-api`

// PhotoView reports paths as "/photos/<rest>" (its container mount root);
// server/api.py expects paths relative to PHOTOS_ROOT ("<rest>"), matching
// what reorganize.py's _resolve_host_path/_photoview_path pair already
// established server-side.
export function toApiPath(photoviewPath: string): string {
  const prefix = '/photos/'
  return photoviewPath.startsWith(prefix) ? photoviewPath.slice(prefix.length) : photoviewPath
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(body),
  })
  if (!resp.ok) {
    const detail = await resp.text()
    throw new Error(`${path} failed (${resp.status}): ${detail}`)
  }
  return resp.json()
}

async function get<T>(path: string): Promise<T> {
  // no-store: this endpoint's data changes on every reorganize call (a
  // photo just moved into a brand new moment needs its path to show up
  // here on the very next sync), but the response carries no explicit
  // Cache-Control from FastAPI, so the browser's default heuristic caching
  // can serve a stale snapshot for an identical GET with no error and no
  // sign anything is wrong - confirmed as the cause of "created a moment,
  // synced with no errors, but it's still not visible": every sync after
  // the first one in a session was silently reading a cached response from
  // before the most recent create/rename.
  const resp = await fetch(`${API_BASE}${path}`, { credentials: 'include', cache: 'no-store' })
  if (!resp.ok) {
    const detail = await resp.text()
    throw new Error(`${path} failed (${resp.status}): ${detail}`)
  }
  return resp.json()
}

// Stable per-photo ids, keyed by PhotoView's own path form - see
// docs/decisions/stable-photo-id.md. sync.ts uses this to resolve each
// timeline entry's durable id instead of keying IndexedDB on PhotoView's
// own id, which changes on every move/rename.
export function fetchPhotoIds(): Promise<Record<string, string>> {
  return get<Record<string, string>>('/photo-ids')
}

export function setFavorite(photoviewPath: string, favorite: boolean) {
  return post<{ ok: true }>('/favorite', { path: toApiPath(photoviewPath), favorite })
}

export function renameMoment(momentPhotoviewPath: string, newLabel: string, newDate?: string) {
  return post<{ ok: true; new_path: string }>('/moments/rename', {
    moment_path: toApiPath(momentPhotoviewPath),
    new_label: newLabel,
    new_date: newDate ?? null,
  })
}

export function mergeMoments(momentPhotoviewPaths: string[], newLabel: string) {
  return post<{ ok: true; new_path: string }>('/moments/merge', {
    moment_paths: momentPhotoviewPaths.map(toApiPath),
    new_label: newLabel,
  })
}

// Opt-in only, never automatic - see docs/decisions/geo-reverse-geocoding.md
export function suggestMomentName(momentPhotoviewPath: string) {
  return post<{ ok: true; suggestion: string | null }>('/moments/suggest-name', {
    moment_path: toApiPath(momentPhotoviewPath),
  })
}

// User-selected photos -> a new moment folder - see docs/decisions/range-select-new-moment.md
export function createMomentFromPhotos(photoPhotoviewPaths: string[], newLabel: string) {
  return post<{ ok: true; new_path: string; moved_count: number }>('/moments/create', {
    photo_paths: photoPhotoviewPaths.map(toApiPath),
    new_label: newLabel,
  })
}

// ---------------------------------------------------------------------------
// First-party media backend - replaces PhotoView's myTimeline + thumbnails.
// See docs/decisions/replacing-photoview.md.
// ---------------------------------------------------------------------------

export interface TimelineEntry {
  id: string
  path: string
  moment: string
  momentPath: string
  date: string
  favorite: boolean
  width: number | null
  height: number | null
  isVideo: boolean
  duration: number | null
  // A still with a clip attached (Live Photo / Motion Photo). Where the
  // video actually lives - a sibling file or embedded in this one - is
  // the server's problem; the client just asks /media/{id}/motion.
  hasMotion: boolean
}

// Paged rather than fetched whole: the server caps `limit`, because at the
// project's ~5TB target a full-library response would be a large JSON
// serialisation held in RAM on a 1GB box. Same loop shape the PhotoView
// client used, so this is not a new pattern - keep going until a page comes
// back shorter than requested.
// Paired with fetchMediaTimelinePaged: a full relist is one request per
// 500 photos (~700KB / 5 round-trips at 2,298 photos - measured 10s over
// Tailscale at 4,000), so sync.ts checks this single cheap number first
// and skips the relist entirely when it matches what was seen last time.
export async function fetchTimelineCount(): Promise<number> {
  const resp = await fetch(`${API_BASE}/timeline/count`, {
    credentials: 'include',
    cache: 'no-store',
  })
  if (!resp.ok) throw new Error(`timeline count failed (${resp.status})`)
  return (await resp.json()).count
}

export async function fetchMediaTimeline(pageSize = 500): Promise<TimelineEntry[]> {
  const all: TimelineEntry[] = []
  await fetchMediaTimelinePaged((page) => {
    all.push(...page)
  }, pageSize)
  return all
}

// Same request loop as fetchMediaTimeline, but hands each page to the
// caller as it arrives instead of buffering the whole library. On a
// library of a few thousand photos that is the difference between the
// grid staying blank for 30-60s (all pages, then one big write) and rows
// appearing every ~100ms as pages land - see sync.ts.
export async function fetchMediaTimelinePaged(
  onPage: (page: TimelineEntry[]) => void | Promise<void>,
  pageSize = 500,
): Promise<void> {
  let offset = 0
  for (;;) {
    // no-store for the same reason /photo-ids needs it: this changes on
    // every reorganize, and a silently-cached GET reproduces the "created a
    // moment, synced fine, still not visible" bug (see CLAUDE.md).
    const resp = await fetch(`${API_BASE}/timeline?limit=${pageSize}&offset=${offset}`, {
      credentials: 'include',
      cache: 'no-store',
    })
    if (!resp.ok) throw new Error(`timeline failed (${resp.status})`)
    const page: TimelineEntry[] = await resp.json()
    await onPage(page)
    if (page.length < pageSize) break
    offset += pageSize
  }
}

// Derivatives are immutable per photo_id and served with a long max-age, so
// these are plain URLs the browser can cache normally - unlike the timeline
// JSON above.
export function thumbUrl(id: string): string {
  return `${API_BASE}/media/${id}/thumb`
}

export function previewUrl(id: string): string {
  return `${API_BASE}/media/${id}/preview`
}

export function originalUrl(id: string): string {
  return `${API_BASE}/media/${id}/original`
}

// An H.264 transcode, where the host has one. Lower resolution than the
// original, so this is only requested after the original fails to play -
// see PhotoViewer's onError handling.
export function compatUrl(id: string): string {
  return `${API_BASE}/media/${id}/compat`
}

export function motionUrl(id: string): string {
  return `${API_BASE}/media/${id}/motion`
}

export async function runIndex(): Promise<{ seen: number; indexed: number; skipped: number }> {
  return post('/index', {})
}

export interface MediaMetadata {
  filename: string
  fileSize: number
  width: number | null
  height: number | null
  isVideo: boolean
  duration: number | null
  dateShot: string | null
  camera: string | null
  maker: string | null
  lens: string | null
  exposure: number | null
  aperture: number | null
  iso: number | null
  focalLength: number | null
  coordinates: { latitude: number; longitude: number } | null
}

// Lazy, per-photo: the panel is opened for a small fraction of photos, so
// this is fetched on open rather than carried in the timeline payload.
export async function fetchMediaMetadata(id: string): Promise<MediaMetadata> {
  const resp = await fetch(`${API_BASE}/media/${id}/metadata`, { credentials: 'include' })
  if (!resp.ok) throw new Error(`metadata failed (${resp.status})`)
  return resp.json()
}

// Auth against our own server, replacing PhotoView's authorizeUser mutation.
// Per-user accounts: each user has their own photo library, and the
// server derives which one from this session cookie (see server/auth.py).
// There is no way for the client to ask for a different user's photos.
//
// No manual cookie handling here, unlike the PhotoView client this replaces:
// PhotoView sent no Set-Cookie at all and expected the client to set the
// cookie itself from the response body. Our /login sets an httponly cookie
// properly, so the browser handles it and page scripts can't read the token.
export async function login(username: string, password: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ username, password }),
  })
  if (!resp.ok) {
    throw new Error(
      resp.status === 401 ? 'wrong username or password' : `login failed (${resp.status})`,
    )
  }
}

// Three outcomes, not two. The server answers the "is this cookie good?"
// question as a 200 carrying authenticated: true|false - so ANY other
// outcome (a network failure, a 502 from a reverse proxy in front of a
// stopped server, a 5xx) is not an answer at all, it just means the
// question couldn't be asked. Collapsing that into authenticated: false
// logged people out every time the NAS was asleep or unreachable, and the
// login form they were then shown could only fail the same way
// ("Login failed: login failed (502)"), so there was no way back in until
// the server returned. `unknown` lets the caller keep the existing session.
export type SessionState =
  | { status: 'authenticated'; username?: string }
  | { status: 'anonymous' }
  | { status: 'unknown'; reason: string }

export async function checkSession(): Promise<SessionState> {
  let resp: Response
  try {
    resp = await fetch(`${API_BASE}/session`, {
      credentials: 'include',
      cache: 'no-store',
      // Without a timeout this waits out the OS's TCP connect timeout -
      // ~10s of blank screen before the cached library appears, every time
      // the app opens away from the server. The answer only gates whether
      // to show the login form; if the server has not replied in 2.5s it
      // is not going to be reachable enough to sync from either, and the
      // offline path (session marker + IndexedDB) is already the right
      // fallback.
      signal: AbortSignal.timeout(2500),
    })
  } catch (err) {
    return { status: 'unknown', reason: err instanceof Error ? err.message : String(err) }
  }
  // 401/403 would be a real "your cookie is no good" answer; everything
  // else in the error range is the server or something in front of it
  // failing to answer.
  if (resp.status === 401 || resp.status === 403) return { status: 'anonymous' }
  if (!resp.ok) return { status: 'unknown', reason: `server unreachable (${resp.status})` }
  try {
    const body = await resp.json()
    return body.authenticated === true
      ? { status: 'authenticated', username: body.username }
      : { status: 'anonymous' }
  } catch {
    // A 200 that isn't our JSON is a captive portal or a proxy error page,
    // not a logged-out answer.
    return { status: 'unknown', reason: 'unexpected session response' }
  }
}

// Clears the session cookie server-side. The local IndexedDB cache is
// deliberately dropped by the caller rather than here: with per-user
// libraries, leaving one user's thumbnails on the device for the next
// person to sync over would be both confusing and a privacy leak.
export async function logout(): Promise<void> {
  await fetch(`${API_BASE}/logout`, { method: 'POST', credentials: 'include' })
}
