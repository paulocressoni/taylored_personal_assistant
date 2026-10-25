/*
 * app_main.c — the entry point of the satellite firmware.
 *
 * ESP-IDF calls app_main() once, after the chip has booted and the FreeRTOS
 * scheduler is running. At this step the firmware joins the Wi-Fi network and
 * reports whether it is online.
 */

#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include "wifi.h"

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

    /* Starting the radio takes a moment; the connection happens later, in the
     * event handler. A failure here means the configuration is wrong (no
     * SSID), not that the network is down. */
    err = wifi_start();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "wifi_start failed: %s", esp_err_to_name(err));
    }

    /* A task must never return: returning from app_main deletes the task.
     * This loop is a placeholder for the satellite state machine that arrives
     * in a later step. */
    for (;;) {
        ESP_LOGI(TAG, "heartbeat: %s", wifi_is_connected() ? "online" : "offline");

        /* Block for 5 s. While blocked, the CPU runs the Wi-Fi and event
         * loop tasks. pdMS_TO_TICKS converts ms to ticks; at the configured
         * 1000 Hz tick rate one tick is one millisecond. */
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}
