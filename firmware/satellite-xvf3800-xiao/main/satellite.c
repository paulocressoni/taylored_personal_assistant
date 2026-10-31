#include "satellite.h"

#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "leds.h"
#include "wifi.h"
#include "ws_client.h"

static const char *TAG = "sat";

/* The supervisor wakes four times a second: fast enough to notice a drop, slow
 * enough to cost nothing. */
#define SUPERVISOR_POLL_MS 250

/* The server drops a socket that sends nothing for 60 s. Audio streaming keeps
 * it alive on its own; this only matters while no audio is flowing. */
#define KEEPALIVE_MS 20000

/* The retry delay doubles after each failure, so a server that is down is not
 * hammered, while a transient drop reconnects within a second. */
#define RECONNECT_MIN_MS 1000
#define RECONNECT_MAX_MS 30000

/* Session state: the server acknowledged the start frame. */
static volatile bool s_ready;

/* Transport ownership: an event handler must never tear the client down,
 * because it runs inside the client's own task and the teardown would wait for
 * that task to end. It records the drop instead; the supervisor acts on it. */
static volatile bool s_down;
static bool s_armed;
static uint32_t s_wait_ms = RECONNECT_MIN_MS;
static int64_t s_retry_at_ms;
static int64_t s_next_keepalive_ms;

static int64_t now_ms(void) {
    return esp_timer_get_time() / 1000;
}

void satellite_device_id(char *out, size_t len) {
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(out, len, "xiao-%02x%02x%02x%02x%02x%02x", mac[0], mac[1], mac[2], mac[3], mac[4],
             mac[5]);
}

bool satellite_is_ready(void) {
    return s_ready;
}

void satellite_on_ws_connected(void) {
    /* Open, but the session is not acknowledged until "ready" arrives. */
    leds_set(LED_STATE_IDLE);
}

void satellite_on_ws_text(const char *json, size_t len) {
    cJSON *root = cJSON_ParseWithLength(json, len);
    if (root == NULL) {
        ESP_LOGW(TAG, "ignoring a control frame that is not JSON");
        return;
    }

    const cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
    if (!cJSON_IsString(type)) {
        cJSON_Delete(root);
        return;
    }

    if (strcmp(type->valuestring, "ready") == 0) {
        s_ready = true;
        s_wait_ms = RECONNECT_MIN_MS;
        s_next_keepalive_ms = now_ms() + KEEPALIVE_MS;
        ESP_LOGI(TAG, "session ready");
        leds_set(LED_STATE_LISTENING);
    } else if (strcmp(type->valuestring, "error") == 0) {
        const cJSON *detail = cJSON_GetObjectItemCaseSensitive(root, "detail");
        ESP_LOGE(TAG, "server error: %s", cJSON_IsString(detail) ? detail->valuestring : "unknown");
    } else {
        ESP_LOGD(TAG, "control frame \"%s\"", type->valuestring);
    }

    cJSON_Delete(root);
}

void satellite_on_ws_down(void) {
    if (s_ready) {
        ESP_LOGW(TAG, "session lost");
    }
    s_ready = false;
    s_down = true;
    leds_set(LED_STATE_IDLE);
}

static void supervisor_task(void *arg) {
    for (;;) {
        const int64_t now = now_ms();

        /* A drop reported by the event handler is cleaned up here, where it is
         * safe to join the client task. */
        if (s_down) {
            s_down = false;
            if (s_armed) {
                ws_stop();
                s_armed = false;
            }
            s_ready = false;
            s_retry_at_ms = now + (int64_t)s_wait_ms;
            ESP_LOGI(TAG, "reconnecting in %u ms", (unsigned)s_wait_ms);
            s_wait_ms = s_wait_ms * 2 <= RECONNECT_MAX_MS ? s_wait_ms * 2 : RECONNECT_MAX_MS;
        }

        if (!wifi_is_connected()) {
            /* No point holding a socket when the radio is down. */
            if (s_armed) {
                ws_stop();
                s_armed = false;
            }
            s_ready = false;
        } else if (!s_armed) {
            if (now >= s_retry_at_ms) {
                const esp_err_t err = ws_start();
                if (err == ESP_OK) {
                    s_armed = true;
                } else {
                    ESP_LOGE(TAG, "could not start the socket: %s", esp_err_to_name(err));
                    s_retry_at_ms = now + (int64_t)s_wait_ms;
                }
            }
        } else if (s_ready && now >= s_next_keepalive_ms) {
            s_next_keepalive_ms = now + KEEPALIVE_MS;
            if (!ws_send_text("{\"type\":\"keepalive\"}")) {
                ESP_LOGW(TAG, "keepalive failed");
            }
        }

        vTaskDelay(pdMS_TO_TICKS(SUPERVISOR_POLL_MS));
    }
}

void satellite_start(void) {
    if (xTaskCreatePinnedToCore(supervisor_task, "satellite", 6144, NULL, 5, NULL, 0) != pdPASS) {
        ESP_LOGE(TAG, "could not start the supervisor task");
    }
}
