/**
 * Reads ONLY the entries out of a ZIP_STORED (no compression) archive -
 * exactly what server/api.py's /media/thumbs-batch produces, and nothing
 * else. Not a general zip reader: no DEFLATE support, no central
 * directory parsing, no zip64. That's a deliberate scope cut, not an
 * oversight - webp is already compressed, so the server never deflates,
 * and pulling in a general-purpose zip library (JSZip, fflate) for a
 * format this constrained would be more code and more attack surface
 * than the ~40 lines here for a format we fully control on both ends.
 *
 * Walks local file headers sequentially from the front, which is valid
 * for ZIP_STORED specifically: each entry's compressed size is known
 * up front (it equals the uncompressed size, since nothing was
 * compressed), so there's no need to consult the central directory at
 * the end of the archive the way a general reader would.
 */

const LOCAL_FILE_HEADER_SIGNATURE = 0x04034b50

export interface ZipEntry {
  name: string
  // Parameterized, not plain `Uint8Array` (which now defaults to the wider
  // `Uint8Array<ArrayBufferLike>`): every instance here is sliced from the
  // `ArrayBuffer` passed into readStoredZip, and callers pass this straight
  // into `new Blob([...])`, whose BlobPart type no longer accepts the
  // widened form.
  data: Uint8Array<ArrayBuffer>
}

export function readStoredZip(bytes: ArrayBuffer): ZipEntry[] {
  const view = new DataView(bytes)
  const entries: ZipEntry[] = []
  let offset = 0

  while (offset + 4 <= view.byteLength) {
    const signature = view.getUint32(offset, true)
    if (signature !== LOCAL_FILE_HEADER_SIGNATURE) break // central directory or end of entries

    const compressionMethod = view.getUint16(offset + 8, true)
    const compressedSize = view.getUint32(offset + 18, true)
    const nameLength = view.getUint16(offset + 26, true)
    const extraLength = view.getUint16(offset + 28, true)

    const nameStart = offset + 30
    const nameBytes = new Uint8Array(bytes, nameStart, nameLength)
    const name = new TextDecoder().decode(nameBytes)

    const dataStart = nameStart + nameLength + extraLength
    if (compressionMethod !== 0) {
      throw new Error(`unsupported compression method ${compressionMethod} for entry ${name} - server must write ZIP_STORED`)
    }
    entries.push({ name, data: new Uint8Array(bytes, dataStart, compressedSize) })

    offset = dataStart + compressedSize
  }

  return entries
}
