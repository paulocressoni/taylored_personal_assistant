const STORAGE_KEY = 'ipa.device_id'

/**
 * Returns this browser's stable device id, creating + persisting one on
 * first use. localStorage survives page reloads and browser restarts, so
 * the id stays constant for this browser profile.
 */
export function getDeviceId(): string {
  let id = localStorage.getItem(STORAGE_KEY)
  if (!id) {
    id = newUuid()
    localStorage.setItem(STORAGE_KEY, id)
  }
  return id
}

function newUuid(): string {
  // crypto.randomUUID() is available on localhost (a "secure context") in
  // all modern browsers.
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  // Fallback for very old browsers: RFC4122-style v4 built from Math.random.
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}
