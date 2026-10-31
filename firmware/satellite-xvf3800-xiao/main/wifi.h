#pragma once

#include <stdbool.h>

#include "esp_err.h"
#include "freertos/FreeRTOS.h"

/**
 * Start the Wi-Fi station and begin connecting.
 *
 * Returns as soon as the radio is started; it does not wait for an IP. Watch
 * the connection with wifi_is_connected() or wifi_wait_connected().
 *
 * @return ESP_OK if the driver started, or ESP_ERR_INVALID_STATE when no SSID
 *         has been configured through menuconfig.
 */
esp_err_t wifi_start(void);

/**
 * Report whether the station currently holds an IP address.
 *
 * @return true when connected, false otherwise.
 */
bool wifi_is_connected(void);

/**
 * Block until the station holds an IP, or the timeout expires.
 *
 * @param timeout Ticks to wait; portMAX_DELAY waits forever.
 * @return true if connected (immediately or within the timeout).
 */
bool wifi_wait_connected(TickType_t timeout);
