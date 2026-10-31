#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/**
 * The identity this device reports to the server.
 *
 * Derived from the Wi-Fi MAC address, so it survives a reflash and stays
 * stable across reboots. The server stores it with each turn.
 *
 * @param out Buffer to fill with a NUL-terminated id.
 * @param len Capacity of `out`.
 */
void satellite_device_id(char *out, size_t len);

/** Start the supervisor task that owns the socket lifecycle. */
void satellite_start(void);

/** Report the session state: the server has acknowledged the start frame. */
bool satellite_is_ready(void);

/**
 * Send one frame of microphone audio to the server.
 *
 * Does nothing until the session is ready. While muted the frame is replaced
 * with silence rather than skipped, so the socket stays alive and the server's
 * endpointer hears nothing to trigger on.
 *
 * @param samples Mono PCM at 16 kHz.
 * @param count   Number of samples.
 * @return true when the frame was sent.
 */
bool satellite_send_audio(const int16_t *samples, size_t count);

/** Flip the mute latch and return the new state. */
bool satellite_toggle_mute(void);

/** Report whether the microphone is muted. */
bool satellite_is_muted(void);

/* Called by the WebSocket client, from its own task. */
void satellite_on_ws_connected(void);
void satellite_on_ws_text(const char *json, size_t len);
void satellite_on_ws_binary(const uint8_t *data, size_t len);
void satellite_on_ws_down(void);
