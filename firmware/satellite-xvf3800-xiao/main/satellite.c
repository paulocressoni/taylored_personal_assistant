#include "satellite.h"

#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "audio_i2s.h"
#include "leds.h"
#include "wifi.h"
#include "ws_client.h"

static const char *TAG = "sat";

/* The supervisor wakes four times a second: fast enough to notice a drop, slow
 * enough to cost nothing. */
#define SUPERVISOR_POLL_MS 250

/* The server drops a socket that sends nothing for 60 s. Streaming audio keeps
 * it alive on its own, so this is only sent when the uplink has gone quiet:
 * pushing a text frame while audio is flowing would contend for the same
 * socket lock and cost frames. */
#define KEEPALIVE_MS 20000

/* The retry delay doubles after each failure, so a server that is down is not
 * hammered, while a transient drop reconnects within a second. */
#define RECONNECT_MIN_MS 1000
#define RECONNECT_MAX_MS 30000

/* Session state: the server acknowledged the start frame. */
static volatile bool s_ready;

/* Microphone mute. The flag lives here because the audio path is what has to
 * honour it; the LED follows it. */
static volatile bool s_muted;

/* Bytes of answer audio received for the current turn, reported on audio_end. */
static volatile size_t s_reply_bytes;

/* When the uplink last carried something, so the keepalive can stay quiet. */
static volatile int64_t s_last_uplink_ms;

/* Transport ownership: an event handler must never tear the client down,
 * because it runs inside the client's own task and the teardown would wait for
 * that task to end. It records the drop instead; the supervisor acts on it. */
static volatile bool s_down;
static bool s_armed;
static uint32_t s_wait_ms = RECONNECT_MIN_MS;
static int64_t s_retry_at_ms;

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

bool satellite_is_muted(void) {
    return s_muted;
}

bool satellite_toggle_mute(void) {
    s_muted = !s_muted;
    return s_muted;
}

bool satellite_send_audio(const int16_t *samples, size_t count) {
    if (!s_ready) {
        return false;
    }

    /* Silence rather than nothing: the server drops a socket that sends
     * nothing for 60 s, and its endpointer must not see the muted audio. */
    static int16_t silence[AUDIO_FRAME_SAMPLES];
    const size_t n = count < AUDIO_FRAME_SAMPLES ? count : AUDIO_FRAME_SAMPLES;

    const bool sent = ws_send_audio(s_muted ? silence : samples, n);
    if (sent) {
        s_last_uplink_ms = now_ms();
    }
    return sent;
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
        s_last_uplink_ms = now_ms();
        ESP_LOGI(TAG, "session ready");
        leds_set(LED_STATE_LISTENING);
    } else if (strcmp(type->valuestring, "transcript") == 0) {
        const cJSON *text = cJSON_GetObjectItemCaseSensitive(root, "text");
        if (cJSON_IsString(text)) {
            ESP_LOGI(TAG, "heard: %s", text->valuestring);
        }
        leds_set(LED_STATE_THINKING);
    } else if (strcmp(type->valuestring, "audio_end") == 0) {
        ESP_LOGI(TAG, "reply audio: %u bytes", (unsigned)s_reply_bytes);
        s_reply_bytes = 0;
        leds_set(s_muted ? LED_STATE_MUTED : LED_STATE_LISTENING);
    } else if (strcmp(type->valuestring, "error") == 0) {
        const cJSON *detail = cJSON_GetObjectItemCaseSensitive(root, "detail");
        ESP_LOGE(TAG, "server error: %s", cJSON_IsString(detail) ? detail->valuestring : "unknown");
    } else {
        ESP_LOGD(TAG, "control frame \"%s\"", type->valuestring);
    }

    cJSON_Delete(root);
}

void satellite_on_ws_binary(const uint8_t *data, size_t len) {
    /* The answer's audio. Playing it is the next step; for now the byte count
     * proves the server's speech came back over the same socket. */
    s_reply_bytes += len;
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
        } else if (s_ready && now - s_last_uplink_ms >= KEEPALIVE_MS) {
            /* Nothing has gone out for 20 s: the uplink is stalled or the
             * capture task is not running. Keep the session alive anyway. */
            s_last_uplink_ms = now;
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
