// frontend/src/lib/uuid.ts
// The ONE place this app creates ids. crypto.randomUUID() needs a secure
// context (localhost / https); where it is unavailable we fall back to an
// RFC 4122 v4 UUID built from Math.random. Kept in its own module so every
// caller shares the same implementation and the fallback is tested once.

/**
 * Return a fresh random (v4) UUID string.
 *
 * Prefers the Web Crypto `crypto.randomUUID()` — available on localhost and
 * https (a "secure context") in all modern browsers. Falls back to a
 * hand-rolled RFC 4122 v4 UUID (Math.random) only when that API is missing.
 *
 * @returns A new UUID string, e.g. "0f8fad5b-d9cb-469f-a165-70867728950e".
 */
export function newUuid(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}
