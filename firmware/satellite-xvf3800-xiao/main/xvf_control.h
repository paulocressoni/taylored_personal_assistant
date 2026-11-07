#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

/**
 * Open the I2C bus and wait for the XVF3800 to answer.
 *
 * The XVF3800 needs about two seconds after power-up before it replies, so
 * this retries for several seconds instead of failing on the first attempt.
 *
 * @return ESP_OK when the device identified itself, or ESP_ERR_TIMEOUT when it
 *         never answered.
 */
esp_err_t xvf_init(void);

/**
 * Read and log the firmware version and build string.
 *
 * Useful as a boot-time record: the I2S audio format depends on which firmware
 * image is running.
 */
void xvf_log_identity(void);

/** Set the LED ring effect: 0 off, 1 breath, 2 rainbow, 3 single, 4 direction. */
esp_err_t xvf_set_led_effect(uint8_t effect);

/** Set the LED ring brightness, 0 to 255. */
esp_err_t xvf_set_led_brightness(uint8_t brightness);

/** Set the colour used by the breath and single-colour effects (0xRRGGBB). */
esp_err_t xvf_set_led_color(uint32_t rgb);

/**
 * Enable or disable the speaker power amplifier.
 *
 * The XVF3800 also treats this pin as a "speaker is active" hint and lowers
 * microphone sensitivity while it is enabled, so it stays off for headphones.
 */
esp_err_t xvf_set_amp_enabled(bool enabled);

/**
 * Hand mute control to the host.
 *
 * Not persisted in the XVF3800's flash: it resets to the built-in behaviour on
 * every boot, so this must be sent again after each power-up.
 */
esp_err_t xvf_disable_mute_function(void);

/**
 * Read the state of the onboard mute button.
 *
 * @param pressed Receives true while the button is held down.
 * @return ESP_OK on a successful read, or an I2C error.
 */
esp_err_t xvf_read_mute_button(bool *pressed);
