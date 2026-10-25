/*
 * app_main.c — the entry point of the satellite firmware.
 *
 * ESP-IDF calls app_main() once, after the chip has booted and the FreeRTOS
 * scheduler is running. At this step the firmware brings up the XVF3800 over
 * I2C, takes over its LED ring, and reports the mute button.
 */

#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include "leds.h"
#include "wifi.h"
#include "xvf_control.h"

/* Every log line is prefixed with this tag, so you can tell which module
 * printed it. Each file will have its own tag. */
static const char *TAG = "sat";

void app_main(void) {
    /* The Wi-Fi driver keeps calibration data in NVS and refuses to start
     * without it. The two error codes mean the partition is from a different
     * layout or a newer ESP-IDF: erasing and re-initialising is the documented
     * recovery. */
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    ESP_LOGI(TAG, "satellite booting");

    /* The audio path depends on the XVF3800, so talk to it before the radio.
     * A failure is reported, not fatal: the log is more useful than a reboot
     * loop, and the reasons are almost always wiring or the wrong firmware. */
    err = xvf_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "XVF3800 unavailable: %s", esp_err_to_name(err));
    }
    xvf_log_identity();

    /* Headphones are on the 3.5 mm jack for the bench, so the speaker
     * amplifier stays off; see xvf_set_amp_enabled for why that also matters
     * to the microphones. */
    if (xvf_set_amp_enabled(false) != ESP_OK) {
        ESP_LOGW(TAG, "could not configure the amplifier");
    }
    /* Without this the mute button mutes the XVF in hardware and the host
     * never learns it was pressed. */
    if (xvf_disable_mute_function() != ESP_OK) {
        ESP_LOGW(TAG, "could not take over mute control");
    }

    leds_init();
    leds_set(LED_STATE_IDLE);

    err = wifi_start();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "wifi_start failed: %s", esp_err_to_name(err));
    }

    bool was_pressed = false;
    for (;;) {
        /* Polling is fine for a button at this rate, and keeps this step free
         * of an interrupt-driven GPIO handler. */
        bool pressed = false;
        if (xvf_read_mute_button(&pressed) == ESP_OK && pressed != was_pressed) {
            was_pressed = pressed;
            ESP_LOGI(TAG, "mute button %s", pressed ? "pressed" : "released");
        }

        leds_set(pressed ? LED_STATE_MUTED : LED_STATE_IDLE);

        ESP_LOGI(TAG, "heartbeat: %s", wifi_is_connected() ? "online" : "offline");
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}
