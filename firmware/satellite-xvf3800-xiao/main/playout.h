#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

/**
 * Allocate the playout ring.
 *
 * The buffer lives in PSRAM: it has to hold a whole spoken answer, because the
 * server synthesises faster than real time and the audio clock drains at
 * exactly 16 kHz.
 *
 * @return ESP_OK, or ESP_ERR_NO_MEM when neither PSRAM nor internal RAM has
 *         room.
 */
esp_err_t playout_init(void);

/**
 * Append samples to the ring.
 *
 * Never blocks. Samples that do not fit are dropped and counted, which is the
 * right trade for audio: a truncated answer is better than a stalled socket.
 *
 * @param samples Mono PCM at 16 kHz.
 * @param count   Number of samples.
 * @return How many samples were stored; fewer than `count` means it overflowed.
 */
size_t playout_write(const int16_t *samples, size_t count);

/**
 * Take samples out of the ring for the speaker.
 *
 * Always fills the buffer: anything missing is filled with silence, so the
 * audio clock keeps running and an empty ring is silent rather than a stall.
 * Playback only begins once a prebuffer has accumulated, so gaps between the
 * server's sentence-sized bursts are ridden out instead of heard.
 *
 * @param out   Destination, `count` samples.
 * @param count Number of samples wanted.
 * @return How many samples were real audio; the rest were silence.
 */
size_t playout_read(int16_t *out, size_t count);

/**
 * Whether the ring is currently being drained.
 *
 * False while it is still filling towards the prebuffer and again after it has
 * run dry, so a caller counting underruns can tell a real gap in an answer from
 * one that has simply not started yet.
 *
 * @return true when `playout_read` is handing out real audio.
 */
bool playout_is_playing(void);

/** Discard everything buffered. This is the barge-in signal. */
void playout_flush(void);

/** How many samples are waiting to be played. */
size_t playout_level(void);

/** Log and reset the underrun/overflow counters. */
void playout_log_stats(void);
