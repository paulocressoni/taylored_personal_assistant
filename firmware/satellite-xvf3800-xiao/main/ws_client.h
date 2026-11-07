#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/**
 * Open the voice socket.
 *
 * Returns as soon as the client task is started; the connection completes in
 * the background. Session state belongs to satellite.h.
 *
 * @return ESP_OK, or an error when the URI or the API key is missing, or the
 *         client could not be created.
 */
esp_err_t ws_start(void);

/** Close the socket and free the client. Safe to call when already stopped. */
void ws_stop(void);

/** Report the transport state: a socket is open. */
bool ws_is_connected(void);

/**
 * Send one control frame as text.
 *
 * @param text NUL-terminated JSON.
 * @return true when it was queued; false when the socket is down.
 */
bool ws_send_text(const char *text);

/**
 * Send one frame of captured audio as a binary message.
 *
 * @param samples Mono PCM at 16 kHz; sent unchanged, with no encoding.
 * @param count   Number of samples.
 * @return true when the frame was accepted by the transport.
 */
bool ws_send_audio(const int16_t *samples, size_t count);
