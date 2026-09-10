import type { PhotoMeta } from './db'

// See docs/decisions/download-to-device.md for the full reasoning: share
// sheet first (works on iOS/Android, the primary target devices, and
// directly serves the "share to WhatsApp" goal in one step), File System
// Access API for a real mirrored folder tree on desktop Chrome/Edge, plain
// <a download> as the universal last-resort fallback.

// Type declarations for the File System Access API - not yet in TypeScript's
// built-in DOM lib as of this project's TS version, so declared minimally
// here rather than pulling in a whole types package for three methods.
interface FileSystemDirectoryHandleLike {
  getDirectoryHandle(name: string, options?: { create?: boolean }): Promise<FileSystemDirectoryHandleLike>
  getFileHandle(name: string, options?: { create?: boolean }): Promise<{
    createWritable(): Promise<{ write(data: Blob): Promise<void>; close(): Promise<void> }>
  }>
}

declare global {
  interface Window {
    showDirectoryPicker?: () => Promise<FileSystemDirectoryHandleLike>
  }
}

// Remembered across the session so repeated downloads don't re-prompt for a
// folder every time - cleared on page reload (no persistence attempted;
// File System Access handles aren't structured-cloneable into IndexedDB in
// a way worth the complexity for this use case).
let rememberedRoot: FileSystemDirectoryHandleLike | null = null

function filenameFromPath(path: string): string {
  return path.split('/').pop() ?? 'photo.jpg'
}

async function tryShare(photo: PhotoMeta, blob: Blob): Promise<boolean> {
  if (!navigator.share || !navigator.canShare) return false
  const file = new File([blob], filenameFromPath(photo.path), { type: blob.type || 'image/jpeg' })
  if (!navigator.canShare({ files: [file] })) return false
  try {
    await navigator.share({ files: [file] })
    return true
  } catch (err) {
    // AbortError = user cancelled the share sheet, not a real failure
    if (err instanceof Error && err.name === 'AbortError') return true
    return false
  }
}

async function tryMirroredSave(photo: PhotoMeta, blob: Blob): Promise<boolean> {
  if (!window.showDirectoryPicker) return false
  try {
    if (!rememberedRoot) {
      rememberedRoot = await window.showDirectoryPicker()
    }
    const momentDir = await rememberedRoot.getDirectoryHandle(photo.moment, { create: true })
    const fileHandle = await momentDir.getFileHandle(filenameFromPath(photo.path), { create: true })
    const writable = await fileHandle.createWritable()
    await writable.write(blob)
    await writable.close()
    return true
  } catch (err) {
    if (err instanceof Error && err.name === 'AbortError') {
      // User cancelled the folder picker (or headless/no-permission
      // environment auto-rejects it, same error) - fall through to the
      // plain <a download> fallback rather than falsely reporting success.
      // Confirmed this branch was previously mis-swallowed as `return
      // true`, which reported "Saved" even when nothing was actually
      // written - caught by testing the full flow, not by review.
      rememberedRoot = null
      return false
    }
    rememberedRoot = null
    return false
  }
}

function plainDownload(photo: PhotoMeta, blob: Blob) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filenameFromPath(photo.path)
  // Must be attached to the document for .click() to reliably trigger a
  // download in all browsers/headless test environments - confirmed by
  // testing: an unattached anchor's .click() silently did nothing.
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export async function shareOrDownloadPhoto(photo: PhotoMeta, blob: Blob): Promise<'shared' | 'saved-mirrored' | 'saved-flat'> {
  if (await tryShare(photo, blob)) return 'shared'
  if (await tryMirroredSave(photo, blob)) return 'saved-mirrored'
  plainDownload(photo, blob)
  return 'saved-flat'
}
