#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

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

/**
 * Allocate the uplink queue.
 *
 * @return ESP_OK, or ESP_ERR_NO_MEM.
 */
esp_err_t satellite_init(void);

/** Start the supervisor task that owns the socket lifecycle. */
void satellite_start(void);

/** Report the session state: the server has acknowledged the start frame. */
bool satellite_is_ready(void);

/**
 * Hand one captured frame to the uplink.
 *
 * Never blocks and never touches the socket: the audio task must not wait on
 * the network. The frame joins a short queue that the uplink task drains.
 *
 * @param samples Mono PCM at 16 kHz.
 * @param count   Number of samples.
 */
void satellite_queue_mic(const int16_t *samples, size_t count);

/**
 * Take queued microphone audio for transmission.
 *
 * @param out Destination, at most `max` samples.
 * @param max Capacity of `out`.
 * @return Samples written; 0 when the queue is empty.
 */
size_t satellite_take_mic(int16_t *out, size_t max);

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

/**
 * Report that the playout ring could not fill a frame.
 *
 * Counted only while a turn is still in flight: the last frame of an answer
 * legitimately runs out mid-frame, and treating that as a fault would hide the
 * real ones.
 */
void satellite_note_audio_shortfall(void);

/** Flip the mute latch and return the new state. */
bool satellite_toggle_mute(void);

/** Report whether the microphone is muted. */
bool satellite_is_muted(void);

/* Called by the WebSocket client, from its own task. */
void satellite_on_ws_connected(void);
void satellite_on_ws_text(const char *json, size_t len);
void satellite_on_ws_binary(const uint8_t *data, size_t len);
void satellite_on_ws_down(void);
