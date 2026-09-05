// frontend/src/lib/deviceId.ts
// Stable per-browser device id used to tell the backend WHICH device a turn
// (or, in M17, an alarm) belongs to.

import { newUuid } from './uuid'

const STORAGE_KEY = 'ipa.device_id'

/**
 * Returns this browser's stable device id, creating + persisting one on
 * first use. localStorage survives page reloads and browser restarts, so
 * the id stays constant for this browser profile.
 *
 * @returns The stable device id string for this browser.
 */
export function getDeviceId(): string {
  let id = localStorage.getItem(STORAGE_KEY)
  if (!id) {
    id = newUuid()
    localStorage.setItem(STORAGE_KEY, id)
  }
  return id
}
