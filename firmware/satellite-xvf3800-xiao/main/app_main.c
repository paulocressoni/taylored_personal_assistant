/*
 * app_main.c — the entry point of the satellite firmware.
 *
 * ESP-IDF calls app_main() once, after the chip has booted and the
 * FreeRTOS scheduler is running. At this step the program does one thing:
 * prove that the toolchain, the USB cable and the board all work.
 */

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

/* Every log line is prefixed with this tag, so you can tell which module
 * printed it. Each file will have its own tag. */
static const char *TAG = "sat";

void app_main(void) {
    /* ESP_LOGI = log at INFO level. It goes to the USB serial console and
     * is what idf.py monitor displays. */
    ESP_LOGI(TAG, "satellite booting");

    /* A task must never return: returning from app_main deletes the task,
     * which is not a useful state for a device that should stay alive. */
    for (;;) {
        ESP_LOGI(TAG, "heartbeat");

        /* Block this task for 2000 ms. pdMS_TO_TICKS converts milliseconds
         * to FreeRTOS ticks; with CONFIG_FREERTOS_HZ=1000, 1 tick = 1 ms.
         * While blocked, the CPU runs other tasks (Wi-Fi, the idle task). */
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}
