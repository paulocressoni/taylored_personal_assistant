#include "wifi.h"

#include <stdio.h>
#include <string.h>

#include "esp_check.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/event_groups.h"

static const char *TAG = "wifi";

/* Set when the station has an IP; cleared the moment the link drops. */
#define WIFI_CONNECTED_BIT BIT0

static EventGroupHandle_t s_events;
static int64_t s_last_log_us;

/*
 * Runs on the default event loop task, so it must never block: no delays and
 * no waiting on locks. It only records state and asks the driver to retry.
 */
static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_events, WIFI_CONNECTED_BIT);

        /* A wrong password retries forever; throttle the log so the console
         * stays readable while the cause is being fixed. */
        int64_t now = esp_timer_get_time();
        if (now - s_last_log_us > 5 * 1000 * 1000) {
            s_last_log_us = now;
            ESP_LOGW(TAG, "not connected yet, retrying");
        }
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *event = (const ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "got ip " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(s_events, WIFI_CONNECTED_BIT);
    }
}

esp_err_t wifi_start(void) {
    if (strlen(CONFIG_SATELLITE_WIFI_SSID) == 0) {
        ESP_LOGE(TAG, "no SSID configured: run idf.py menuconfig");
        return ESP_ERR_INVALID_STATE;
    }

    s_events = xEventGroupCreate();
    if (s_events == NULL) {
        return ESP_ERR_NO_MEM;
    }

    ESP_RETURN_ON_ERROR(esp_netif_init(), TAG, "netif init");
    ESP_RETURN_ON_ERROR(esp_event_loop_create_default(), TAG, "event loop");
    if (esp_netif_create_default_wifi_sta() == NULL) {
        return ESP_FAIL;
    }

    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_RETURN_ON_ERROR(esp_wifi_init(&init_cfg), TAG, "wifi init");
    ESP_RETURN_ON_ERROR(
        esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL), TAG,
        "wifi handler");
    ESP_RETURN_ON_ERROR(
        esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL), TAG,
        "ip handler");

    wifi_config_t wifi_cfg = {0};
    snprintf((char *)wifi_cfg.sta.ssid, sizeof(wifi_cfg.sta.ssid), "%s",
             CONFIG_SATELLITE_WIFI_SSID);
    snprintf((char *)wifi_cfg.sta.password, sizeof(wifi_cfg.sta.password), "%s",
             CONFIG_SATELLITE_WIFI_PASSWORD);

    /* An empty password means an open network; anything else requires WPA2
     * or better, so a weak or WEP AP is refused instead of silently failing. */
    wifi_cfg.sta.threshold.authmode =
        strlen(CONFIG_SATELLITE_WIFI_PASSWORD) ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    wifi_cfg.sta.pmf_cfg.capable = true;
    wifi_cfg.sta.pmf_cfg.required = false;

    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "set mode");
    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg), TAG, "set config");
    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "start");

    /* Power save parks the radio between beacons. Streaming audio cannot
     * tolerate that jitter, so it is disabled from the start. */
    ESP_RETURN_ON_ERROR(esp_wifi_set_ps(WIFI_PS_NONE), TAG, "power save");

    ESP_LOGI(TAG, "connecting to \"%s\"", CONFIG_SATELLITE_WIFI_SSID);
    return ESP_OK;
}

bool wifi_is_connected(void) {
    if (s_events == NULL) {
        return false;
    }
    return (xEventGroupGetBits(s_events) & WIFI_CONNECTED_BIT) != 0;
}

bool wifi_wait_connected(TickType_t timeout) {
    if (s_events == NULL) {
        return false;
    }
    EventBits_t bits = xEventGroupWaitBits(s_events, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, timeout);
    return (bits & WIFI_CONNECTED_BIT) != 0;
}
