// A stand-in for server/api.py that runs entirely in the page, used only by
// the static GitHub Pages demo (built with VITE_DEMO=1, see
// client/web/scripts/build-demo.sh). It patches window.fetch so every call
// the real client makes to /lesphotos-api/* is answered from a snapshot taken
// off a real server by tools/export_demo.py, so the UI under test is the
// production client, unmodified.
//
// Edits (favorite, rename, merge, create, delete) are applied to an in-memory
// copy of the timeline and persisted to localStorage, so they survive a
// reload but never leave the visitor's browser.

const API = '/lesphotos-api'
const STORE_KEY = 'lesphotos-demo-state-v1'
const REPO = 'https://github.com/barton-muller/lesphotos'

interface Row {
  id: string
  path: string
  moment: string
  momentPath: string
  momentDate: string | null
  date: string | null
  favorite: boolean
  width: number | null
  height: number | null
  isVideo: boolean
  duration: number | null
  hasMotion: boolean
  changedSeq: number
}

type Saved = { seq: number; rows: Record<string, Partial<Row>> }

const dataUrl = (p: string) => `${location.origin}${import.meta.env.BASE_URL}demo-data/${p}`
const stripRoot = (p: string) => (p.startsWith('/photos/') ? p.slice('/photos/'.length) : p)

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
  })
}
const fail = (status: number, detail: string) => json({ detail }, status)

// --- ZIP_STORED writer: the inverse of src/miniZip.ts's reader ---------------

let crcTable: Uint32Array | null = null
function crc32(bytes: Uint8Array): number {
  if (!crcTable) {
    crcTable = new Uint32Array(256)
    for (let n = 0; n < 256; n++) {
      let c = n
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
      crcTable[n] = c >>> 0
    }
  }
  let c = 0xffffffff
  for (let i = 0; i < bytes.length; i++) c = crcTable[(c ^ bytes[i]) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

function zipStored(entries: { name: string; data: Uint8Array }[]): Uint8Array<ArrayBuffer> {
  const enc = new TextEncoder()
  const parts: Uint8Array[] = []
  const central: Uint8Array[] = []
  let offset = 0
  for (const e of entries) {
    const name = enc.encode(e.name)
    const crc = crc32(e.data)
    const local = new DataView(new ArrayBuffer(30))
    local.setUint32(0, 0x04034b50, true)
    local.setUint16(4, 20, true)
    local.setUint16(10, 0, true)
    local.setUint16(12, 0x21, true)
    local.setUint32(14, crc, true)
    local.setUint32(18, e.data.length, true)
    local.setUint32(22, e.data.length, true)
    local.setUint16(26, name.length, true)
    parts.push(new Uint8Array(local.buffer), name, e.data)

    const cd = new DataView(new ArrayBuffer(46))
    cd.setUint32(0, 0x02014b50, true)
    cd.setUint16(4, 20, true)
    cd.setUint16(6, 20, true)
    cd.setUint16(14, 0x21, true)
    cd.setUint32(16, crc, true)
    cd.setUint32(20, e.data.length, true)
    cd.setUint32(24, e.data.length, true)
    cd.setUint16(28, name.length, true)
    cd.setUint32(42, offset, true)
    central.push(new Uint8Array(cd.buffer), name)
    offset += 30 + name.length + e.data.length
  }
  const cdSize = central.reduce((n, p) => n + p.length, 0)
  const end = new DataView(new ArrayBuffer(22))
  end.setUint32(0, 0x06054b50, true)
  end.setUint16(8, entries.length, true)
  end.setUint16(10, entries.length, true)
  end.setUint32(12, cdSize, true)
  end.setUint32(16, offset, true)
  const all = [...parts, ...central, new Uint8Array(end.buffer)]
  const out = new Uint8Array(new ArrayBuffer(all.reduce((n, p) => n + p.length, 0)))
  let pos = 0
  for (const p of all) {
    out.set(p, pos)
    pos += p.length
  }
  return out
}

// --- the library -------------------------------------------------------------

export async function installDemoServer(): Promise<void> {
  const realFetch = window.fetch.bind(window)
  let rows: Row[]
  let metadata: Record<string, unknown>
  try {
    ;[rows, metadata] = await Promise.all([
      realFetch(dataUrl('timeline.json')).then((r) => r.json() as Promise<Row[]>),
      realFetch(dataUrl('metadata.json')).then((r) => r.json() as Promise<Record<string, unknown>>),
    ])
  } catch {
    // Offline on a revisit: leave fetch alone so API calls fail like a
    // genuinely unreachable server and the app shows its cached library.
    mountBanner()
    return
  }

  let seq = 1
  try {
    const saved = JSON.parse(localStorage.getItem(STORE_KEY) ?? 'null') as Saved | null
    if (saved) {
      seq = saved.seq
      for (const row of rows) Object.assign(row, saved.rows[row.id])
    }
  } catch {
    // Private mode or a corrupt value: start from the pristine snapshot.
  }

  const touched = new Set<string>()
  function persist() {
    const saved: Saved = { seq, rows: {} }
    for (const row of rows) {
      if (touched.has(row.id) || row.changedSeq > 1) {
        saved.rows[row.id] = {
          path: row.path, moment: row.moment, momentPath: row.momentPath,
          momentDate: row.momentDate, favorite: row.favorite, changedSeq: row.changedSeq,
        }
      }
    }
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(saved))
    } catch {
      // Edits just won't survive a reload.
    }
  }

  const isTrashed = (r: Row) => r.path.startsWith('/photos/trash/')
  const fileName = (r: Row) => r.path.slice(r.path.lastIndexOf('/') + 1)
  const folderDate = (folder: string) => /^\d{4}-\d{2}-\d{2}/.exec(folder)?.[0] ?? null
  const lastSegment = (p: string) => p.slice(p.lastIndexOf('/') + 1)

  function move(r: Row, momentPath: string, momentDate: string | null) {
    r.path = `/photos/${momentPath}/${fileName(r)}`
    r.momentPath = momentPath
    r.moment = lastSegment(momentPath)
    r.momentDate = momentDate
    r.changedSeq = ++seq
    touched.add(r.id)
  }

  function destFor(date: string, label: string): string {
    return `${date.slice(0, 4)}/${date}-${label}`
  }

  const display = () =>
    rows
      .filter((r) => !isTrashed(r))
      .sort((a, b) => {
        const am = a.momentDate ?? ''
        const bm = b.momentDate ?? ''
        if (am !== bm) return am < bm ? 1 : -1
        const ad = a.date ?? ''
        const bd = b.date ?? ''
        if (ad !== bd) return ad < bd ? 1 : -1
        return a.id < b.id ? -1 : 1
      })

  async function asset(kind: 'thumbs' | 'previews', id: string): Promise<Response> {
    const upstream = await realFetch(dataUrl(`${kind}/${id}.webp`))
    if (!upstream.ok) return fail(404, 'not found')
    return new Response(await upstream.arrayBuffer(), {
      headers: { 'Content-Type': 'image/webp', 'Cache-Control': 'private, max-age=31536000, immutable' },
    })
  }

  async function handle(method: string, path: string, q: URLSearchParams, body: unknown): Promise<Response> {
    const b = (body ?? {}) as Record<string, unknown>
    if (method === 'GET' && path === '/health') return json({ ok: true, version: 'demo' })
    if (method === 'GET' && path === '/session') return json({ authenticated: true, username: 'demo' })
    if (method === 'POST' && path === '/login') return json({ ok: true, username: 'demo' })
    if (method === 'POST' && path === '/logout') return json({ ok: true })

    if (method === 'GET' && path === '/timeline/count') return json({ count: display().length })
    if (method === 'GET' && path === '/timeline/max-seq') return json({ maxSeq: seq })
    if (method === 'GET' && path === '/photo-ids') {
      return json(Object.fromEntries(rows.map((r) => [r.path, r.id])))
    }

    if (method === 'GET' && path === '/timeline') {
      const limit = Math.min(Number(q.get('limit') ?? 500), 2000)
      const since = q.get('since')
      if (since !== null) {
        const items = rows
          .filter((r) => r.changedSeq > Number(since))
          .sort((a, c) => a.changedSeq - c.changedSeq)
          .slice(0, limit)
        return json({ items, maxSeq: items.length ? items[items.length - 1].changedSeq : Number(since) })
      }
      const offset = Number(q.get('offset') ?? 0)
      return json(display().slice(offset, offset + limit).map(({ changedSeq: _drop, ...rest }) => rest))
    }

    const media = /^\/media\/([0-9a-f-]{36})\/(thumb|preview|original|metadata|compat|motion)$/.exec(path)
    if (method === 'GET' && media) {
      const [, id, kind] = media
      if (kind === 'thumb') return asset('thumbs', id)
      if (kind === 'preview' || kind === 'original') return asset('previews', id)
      if (kind === 'metadata') return metadata[id] ? json(metadata[id]) : fail(404, 'not found')
      return fail(404, 'not found')
    }
    if (method === 'GET' && path === '/media/thumb-shards') return json({ shards: [] })
    if (method === 'POST' && path === '/media/thumbs-batch') {
      const ids = ((b.photo_ids as string[]) ?? []).filter((id) => /^[0-9a-f-]{36}$/.test(id))
      const got = await Promise.all(
        ids.map(async (id) => {
          const r = await realFetch(dataUrl(`thumbs/${id}.webp`))
          return r.ok ? { name: id, data: new Uint8Array(await r.arrayBuffer()) } : null
        }),
      )
      return new Response(zipStored(got.filter((e) => e !== null)), {
        headers: { 'Content-Type': 'application/zip', 'Cache-Control': 'no-store' },
      })
    }

    if (method === 'POST' && path === '/favorite') {
      const row = rows.find((r) => r.path === `/photos/${stripRoot(String(b.path))}`)
      if (!row) return fail(409, 'photo not found')
      row.favorite = Boolean(b.favorite)
      row.changedSeq = ++seq
      touched.add(row.id)
      persist()
      return json({ ok: true })
    }

    if (method === 'POST' && path === '/moments/rename') {
      const old = stripRoot(String(b.moment_path))
      const label = String(b.new_label ?? '').trim()
      const members = rows.filter((r) => r.momentPath === old)
      if (!members.length) return fail(409, 'moment not found')
      if (!label) return fail(400, 'label required')
      const date = (b.new_date as string | null) ?? members[0].momentDate ?? folderDate(lastSegment(old))
      if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) return fail(400, 'new_date must be YYYY-MM-DD')
      const dest = destFor(date, label)
      if (dest !== old && rows.some((r) => r.momentPath === dest)) return fail(409, 'a moment with that name exists')
      members.forEach((r) => move(r, dest, date))
      persist()
      return json({ ok: true, new_path: dest })
    }

    if (method === 'POST' && path === '/moments/merge') {
      const olds = ((b.moment_paths as string[]) ?? []).map(stripRoot)
      const label = String(b.new_label ?? '').trim()
      const members = rows.filter((r) => olds.includes(r.momentPath))
      if (!members.length) return fail(409, 'moments not found')
      if (!label) return fail(400, 'label required')
      const date = members.map((r) => r.momentDate ?? '').filter(Boolean).sort()[0]
      const dest = destFor(date, label)
      members.forEach((r) => move(r, dest, date))
      persist()
      return json({ ok: true, new_path: dest })
    }

    if (method === 'POST' && path === '/moments/create') {
      const paths = ((b.photo_paths as string[]) ?? []).map((p) => `/photos/${stripRoot(p)}`)
      const label = String(b.new_label ?? '').trim()
      const members = rows.filter((r) => paths.includes(r.path))
      if (!members.length) return fail(400, 'no photos selected')
      if (!label) return fail(400, 'label required')
      const date =
        (b.new_date as string | null) ?? members.map((r) => r.momentDate ?? '').filter(Boolean).sort()[0]
      if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return fail(400, 'new_date must be YYYY-MM-DD')
      const dest = destFor(date, label)
      members.forEach((r) => move(r, dest, date))
      persist()
      return json({ ok: true, new_path: dest, moved_count: members.length, scan_pending: false })
    }

    if (method === 'POST' && path === '/photos/delete') {
      const paths = ((b.photo_paths as string[]) ?? []).map((p) => `/photos/${stripRoot(p)}`)
      const members = rows.filter((r) => paths.includes(r.path))
      for (const r of members) {
        const day = (r.date ?? '').slice(0, 10) || '1970-01-01'
        move(r, `trash/${day.slice(0, 4)}/${day}`, day)
      }
      persist()
      return json({ ok: true, moved_count: members.length })
    }

    if (method === 'POST' && (path === '/moments/suggest-name' || path === '/moments/suggest-name-for-photos')) {
      return json({ ok: true, suggestion: null })
    }
    return fail(501, 'not available in the browser demo')
  }

  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const u = new URL(url, location.href)
    if (u.origin !== location.origin || !u.pathname.startsWith(`${API}/`)) return realFetch(input, init)
    const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase()
    let body: unknown
    if (typeof init?.body === 'string') {
      try {
        body = JSON.parse(init.body)
      } catch {
        body = undefined
      }
    }
    return handle(method, u.pathname.slice(API.length), u.searchParams, body)
  }

  mountBanner()
}

function mountBanner() {
  const wrap = document.createElement('div')
  wrap.style.cssText =
    'position:fixed;right:12px;bottom:44px;z-index:2147483000;font:13px/1.4 system-ui,sans-serif;' +
    'color:#1d1d1b;text-align:right'
  const pill = document.createElement('button')
  pill.textContent = 'Demo'
  pill.setAttribute('aria-expanded', 'false')
  pill.style.cssText =
    'border:1px solid #cfccc1;background:#fffcf0;color:#1d1d1b;border-radius:999px;padding:6px 12px;' +
    'font:inherit;font-weight:600;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.15)'
  const panel = document.createElement('div')
  panel.hidden = true
  panel.style.cssText =
    'margin-bottom:8px;width:min(300px,calc(100vw - 24px));background:#fffcf0;border:1px solid #cfccc1;' +
    'border-radius:10px;padding:12px 14px;text-align:left;box-shadow:0 8px 24px rgba(0,0,0,.2)'
  panel.innerHTML =
    '<b>lesphotos demo</b><br>The real app, running entirely in your browser: this page plays the part of ' +
    'the server. Edits stay on this device.<br><br>' +
    `<a href="${REPO}">Source</a> · <a href="${REPO}/blob/master/docs/images/CREDITS.md">Photo credits</a> · ` +
    '<a href="#" id="demo-reset">Reset demo</a>'
  panel.querySelectorAll('a').forEach((a) => {
    a.style.color = '#24837b'
  })
  panel.querySelector<HTMLAnchorElement>('#demo-reset')!.addEventListener('click', (e) => {
    e.preventDefault()
    try {
      localStorage.removeItem(STORE_KEY)
    } catch {
      // nothing to clear
    }
    const del = indexedDB.deleteDatabase('lesphotos')
    del.onsuccess = del.onerror = del.onblocked = () => location.reload()
  })
  pill.addEventListener('click', () => {
    panel.hidden = !panel.hidden
    pill.setAttribute('aria-expanded', String(!panel.hidden))
  })
  wrap.append(panel, pill)
  document.body.append(wrap)
}
