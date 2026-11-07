#pragma once

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "freertos/FreeRTOS.h"

/* The server contract: 16 kHz, mono, signed 16-bit, 20 ms frames. */
#define AUDIO_SAMPLE_RATE 16000
#define AUDIO_FRAME_MS 20
#define AUDIO_FRAME_SAMPLES (AUDIO_SAMPLE_RATE / 1000 * AUDIO_FRAME_MS)
#define AUDIO_FRAME_BYTES (AUDIO_FRAME_SAMPLES * sizeof(int16_t))

/**
 * Configure the I2S port and start both directions.
 *
 * The XVF3800 runs the I2S slave image, so the ESP32 generates BCLK and WS.
 * Both channels are enabled because they share those clocks in full duplex.
 *
 * @return ESP_OK, or the first error from the I2S driver.
 */
esp_err_t audio_init(void);

/**
 * Read one frame, converting the 32-bit I2S slots to 16-bit samples.
 *
 * @param left  Receives the left slot, AUDIO_FRAME_SAMPLES samples.
 * @param right Optional; receives the right slot, or NULL to discard it.
 * @param count Samples to read, at most AUDIO_FRAME_SAMPLES.
 * @param timeout How long to wait for the DMA buffer.
 * @return ESP_OK, or an I2S error. On error both buffers are zeroed.
 */
esp_err_t audio_read(int16_t *left, int16_t *right, size_t count, TickType_t timeout);

/**
 * Write one frame to both I2S slots.
 *
 * @param samples Mono samples; each is copied to the left and right slot.
 * @param count   Samples to write, at most AUDIO_FRAME_SAMPLES.
 * @param timeout How long to wait for room in the DMA buffer.
 * @return ESP_OK, or an I2S error.
 */
esp_err_t audio_write(const int16_t *samples, size_t count, TickType_t timeout);
