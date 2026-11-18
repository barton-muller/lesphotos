# Decision: browser storage guards for the thumbnail cache

## Request

"How much would IndexedDB be for a 100GB library?" - followed by agreement to add the two
safety measures that fell out of the answer.

## The sizing question

Thumbnails scale with **file count, not bytes**, and that distinction dominates the answer.
A 25MB video and a 3MB photo each cost exactly one ~20KB thumbnail.

Measured on the real library (`/Volumes/home/Photos`, i.e. the DS124 NAS):

| | value | source |
|---|---|---|
| thumbnail mean | **20.0KB** | n=2,041 real photos (>=1000px); the synthetic 160x120 fakes average 0.2KB and were excluded as they skew it low |
| metadata row | ~400B | ~300B of fields plus IndexedDB row/index overhead |
| photo mean size | 3.31MB | n=4,080 |
| video mean size | 24.60MB | n=362 |
| **video share of files** | **8%** | but 40% of bytes |
| weighted mean file | **5.06MB** | |

100GB / 5.06MB ~= 20,250 files x 20.4KB ~= **390MB**.

| library | files | IndexedDB |
|---|---|---|
| today (21.9GB real) | 4,436 | 90MB |
| **100GB (one user)** | 20,254 | **390MB** |
| 200GB (two users) | 40,508 | 790MB |
| 500GB | 101,270 | 1.97GB |
| 5TB | ~1,037,000 | 20.2GB |

Sensitivity matters, because the figure is driven by average file size:

| avg file | files in 100GB | thumbnails |
|---|---|---|
| 2MB (older phones) | 51,200 | 0.98GB |
| 3.3MB (photos only) | 31,030 | 0.59GB |
| **5MB (this library's actual mix)** | **20,480** | **0.39GB** |
| 8MB (more video) | 12,800 | 0.24GB |

A video-heavy library is *much* cheaper per GB. 390MB is right for this library's composition;
a same-sized library of older, smaller photos would be ~2.5x more.

## Against browser quota

Measured in Chromium on this machine: **3.04GB quota**, `persisted: false`.

Quotas are typically a percentage of free disk (Chrome ~60%; Safari far stingier, often ~1GB
on iOS and aggressive about eviction).

- 100GB/user - **comfortable**, 390MB of a 3GB quota, survives iOS
- 200GB - fine, 790MB
- 500GB - marginal, 1.97GB is most of the quota and would likely fail on iOS
- 5TB - not possible in any browser

## Decision

**No windowing yet.** The library is 21.9GB today and the stated goal is 100GB/user, which
projects to 390MB. The user's own assessment - "it will be several years before my library
reaches 100GB so very safe" - matches the arithmetic. Fetch-everything stays correct.

Two cheap guards added instead, because both failure modes are *silent* today:

**1. `navigator.storage.persist()`** (`storage.ts`, called once per sync from `sync.ts`).
Without it, storage is best-effort and the browser may discard the whole cache under disk
pressure with no warning and no event - the user would just find an empty grid and a full
resync running. Best-effort by design: Chrome grants silently given engagement history,
Firefox prompts, Safari ignores it on iOS. A `false` return is not an error; it means the
cache stays eviction-eligible, exactly as it behaved before.

**2. A pre-sync quota check** (`hasRoomFor`). Exceeding quota throws `QuotaExceededError` from
whichever `db.photos.put()` happens to be running, mid-sync, saying nothing useful. The check
sizes the pending work at ~20KB/thumbnail and refuses up front with the real numbers.

The 0.9 headroom is deliberate: browsers enforce quota approximately and some evict other
origins before hard-failing, so aiming for the last 10% trades a clear message for a
mid-write crash. A browser reporting no quota (older Safari) returns `true` - refusing to sync
on missing information is worse than trying and reporting a real failure.

## Verified

- Normal sync unaffected: 2,330 photos, 42.1MB used of a 3.04GB quota, 0 errors.
- `persist()` declining is handled: headless Chromium has no engagement history and returns
  `false`; sync proceeds normally.
- Quota guard forced by stubbing `navigator.storage.estimate()` to report 2.9GB/3.0GB used:
  fires correctly, page does not crash, and the user sees
  `not enough browser storage: need ~46MB more, 2.7GB of 2.8GB already used`.

## When to revisit

Around 300-500GB, or if `getQuota().ratio` starts reporting >0.5 in practice. The fix then is
**windowing** - recent months resident, older thumbnails fetched on demand and LRU-evicted.
`fullres.ts` already implements exactly that pattern for full-resolution images, so it is a
port rather than a new design.
