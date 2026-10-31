#include "satellite.h"

#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "audio_i2s.h"
#include "freertos/semphr.h"
#include "leds.h"
#include "playout.h"
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

/* How often the audio counters are reported. */
#define STATS_MS 60000

/* The uplink queue is deliberately short. It exists to absorb the windows when
 * the WebSocket client holds its lock delivering answer audio, not to buffer
 * minutes of speech: every queued frame is speech the server hears that much
 * later, and barge-in depends on low uplink latency. */
#define UPLINK_FRAMES 12

/* Session state: the server acknowledged the start frame. */
static volatile bool s_ready;

/* Microphone mute. The flag lives here because the audio path is what has to
 * honour it; the LED follows it. */
static volatile bool s_muted;

/* Bytes of answer audio received for the current turn, reported on audio_end. */
static volatile size_t s_reply_bytes;

/* When the uplink last carried something, so the keepalive can stay quiet. */
static volatile int64_t s_last_uplink_ms;

/* Microphone frames waiting to be sent. Filled by the capture task, drained by
 * the uplink task; the two never wait for each other. */
static int16_t *s_uplink;
static size_t s_uplink_head;
static size_t s_uplink_tail;
static SemaphoreHandle_t s_uplink_lock;
static uint32_t s_uplink_overflow;

/* Answer audio has started arriving for the current turn, and the turn has not
 * finished yet: the window in which a dry playout ring is a real fault. */
static volatile bool s_reply_in_flight;
static uint32_t s_playout_shortfalls;

/* Set when the server says the answer is complete. The LED returns to listening
 * once the ring has actually drained, not when the last frame arrives: the
 * speaker may still be several seconds behind. */
static volatile bool s_audio_end_pending;

/* Transport ownership: an event handler must never tear the client down,
 * because it runs inside the client's own task and the teardown would wait for
 * that task to end. It records the drop instead; the supervisor acts on it. */
static volatile bool s_down;
static bool s_armed;
static uint32_t s_wait_ms = RECONNECT_MIN_MS;
static int64_t s_retry_at_ms;
static int64_t s_next_stats_ms;

static int64_t now_ms(void) {
    return esp_timer_get_time() / 1000;
}

esp_err_t satellite_init(void) {
    const size_t capacity = UPLINK_FRAMES * AUDIO_FRAME_SAMPLES;
    s_uplink = heap_caps_malloc(capacity * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_uplink == NULL) {
        s_uplink = heap_caps_malloc(capacity * sizeof(int16_t), MALLOC_CAP_INTERNAL);
    }
    if (s_uplink == NULL) {
        ESP_LOGE(TAG, "no memory for the uplink queue");
        return ESP_ERR_NO_MEM;
    }

    s_uplink_lock = xSemaphoreCreateMutex();
    if (s_uplink_lock == NULL) {
        heap_caps_free(s_uplink);
        s_uplink = NULL;
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "uplink: %u frames (%u ms)", (unsigned)UPLINK_FRAMES,
             (unsigned)(UPLINK_FRAMES * AUDIO_FRAME_MS));
    return ESP_OK;
}

void satellite_queue_mic(const int16_t *samples, size_t count) {
    if (s_uplink == NULL || count > AUDIO_FRAME_SAMPLES) {
        return;
    }

    xSemaphoreTake(s_uplink_lock, portMAX_DELAY);
    const size_t used = (s_uplink_head + UPLINK_FRAMES - s_uplink_tail) % UPLINK_FRAMES;
    if (used + 1 >= UPLINK_FRAMES) {
        /* Full means the network is behind. Dropping the oldest frame keeps the
         * audio the server hears continuous, which matters more than covering
         * every instant. */
        s_uplink_tail = (s_uplink_tail + 1) % UPLINK_FRAMES;
        s_uplink_overflow++;
    }
    memcpy(&s_uplink[s_uplink_head * AUDIO_FRAME_SAMPLES], samples, count * sizeof(int16_t));
    s_uplink_head = (s_uplink_head + 1) % UPLINK_FRAMES;
    xSemaphoreGive(s_uplink_lock);
}

size_t satellite_take_mic(int16_t *out, size_t max) {
    if (s_uplink == NULL || max < AUDIO_FRAME_SAMPLES) {
        return 0;
    }

    size_t taken = 0;
    xSemaphoreTake(s_uplink_lock, portMAX_DELAY);
    if (s_uplink_tail != s_uplink_head) {
        memcpy(out, &s_uplink[s_uplink_tail * AUDIO_FRAME_SAMPLES],
               AUDIO_FRAME_SAMPLES * sizeof(int16_t));
        s_uplink_tail = (s_uplink_tail + 1) % UPLINK_FRAMES;
        taken = AUDIO_FRAME_SAMPLES;
    }
    xSemaphoreGive(s_uplink_lock);
    return taken;
}

void satellite_note_audio_shortfall(void) {
    /* Only a fault while the server is still sending: at the end of an answer
     * the ring is supposed to run out, and the last frame is always partial. */
    if (s_reply_in_flight) {
        s_playout_shortfalls++;
    }
}

static void clear_uplink(void) {
    if (s_uplink == NULL) {
        return;
    }
    xSemaphoreTake(s_uplink_lock, portMAX_DELAY);
    s_uplink_head = 0;
    s_uplink_tail = 0;
    xSemaphoreGive(s_uplink_lock);
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
        s_reply_in_flight = false;
        clear_uplink();
        ESP_LOGI(TAG, "session ready");
        leds_set(LED_STATE_LISTENING);
    } else if (strcmp(type->valuestring, "transcript") == 0) {
        const cJSON *text = cJSON_GetObjectItemCaseSensitive(root, "text");
        if (cJSON_IsString(text)) {
            ESP_LOGI(TAG, "heard: %s", text->valuestring);
        }
        leds_set(LED_STATE_THINKING);
    } else if (strcmp(type->valuestring, "speech_start") == 0) {
        /* Barge-in. The server heard the user start talking, so everything
         * still queued for the speaker is already obsolete: discard it now so
         * the ring does not talk over them. */
        playout_flush();
        s_audio_end_pending = false;
        s_reply_in_flight = false;
        leds_set(s_muted ? LED_STATE_MUTED : LED_STATE_LISTENING);
    } else if (strcmp(type->valuestring, "audio_end") == 0) {
        ESP_LOGI(TAG, "reply audio: %u bytes", (unsigned)s_reply_bytes);
        s_reply_bytes = 0;
        /* Not idle yet: the ring may still hold seconds of speech. The
         * supervisor flips the LED once it is empty. */
        s_reply_in_flight = false;
        s_audio_end_pending = true;
    } else if (strcmp(type->valuestring, "error") == 0) {
        const cJSON *detail = cJSON_GetObjectItemCaseSensitive(root, "detail");
        ESP_LOGE(TAG, "server error: %s", cJSON_IsString(detail) ? detail->valuestring : "unknown");
    } else {
        ESP_LOGD(TAG, "control frame \"%s\"", type->valuestring);
    }

    cJSON_Delete(root);
}

void satellite_on_ws_binary(const uint8_t *data, size_t len) {
    static int16_t pcm[AUDIO_FRAME_SAMPLES];
    size_t offset = 0;

    /* The wire format is little-endian signed 16-bit, which is the ESP32's
     * native layout, so copying the bytes into an int16_t array is the whole
     * conversion. Copied rather than cast because `data` has no alignment
     * guarantee, and an unaligned 16-bit load traps on some cores. */
    while (offset + sizeof(int16_t) <= len) {
        size_t bytes = len - offset;
        if (bytes > sizeof(pcm)) {
            bytes = sizeof(pcm);
        }
        const size_t samples = bytes / sizeof(int16_t);
        memcpy(pcm, data + offset, samples * sizeof(int16_t));

        /* Silently drops what does not fit. A truncated answer is a much
         * smaller problem than blocking the socket task. */
        playout_write(pcm, samples);
        offset += samples * sizeof(int16_t);
    }

    s_reply_bytes += len;
    s_reply_in_flight = true;
    s_audio_end_pending = false;
    leds_set(s_muted ? LED_STATE_MUTED : LED_STATE_SPEAKING);
}

void satellite_on_ws_down(void) {
    if (s_ready) {
        ESP_LOGW(TAG, "session lost");
    }
    s_ready = false;
    s_down = true;
    s_audio_end_pending = false;
    s_reply_in_flight = false;
    playout_flush();
    clear_uplink();
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

        /* Audio faults are invisible to the listener beyond "it stuttered",
         * so they are counted and reported once a minute. */
        if (now >= s_next_stats_ms) {
            s_next_stats_ms = now + STATS_MS;
            playout_log_stats();
            if (s_uplink_overflow > 0 || s_playout_shortfalls > 0) {
                ESP_LOGW(TAG, "uplink overflow=%u, playout shortfalls=%u",
                         (unsigned)s_uplink_overflow, (unsigned)s_playout_shortfalls);
                s_uplink_overflow = 0;
                s_playout_shortfalls = 0;
            }
        }

        /* The answer has been fully delivered and fully played. */
        if (s_audio_end_pending && playout_level() == 0) {
            s_audio_end_pending = false;
            leds_set(s_muted ? LED_STATE_MUTED : LED_STATE_LISTENING);
        }

        vTaskDelay(pdMS_TO_TICKS(SUPERVISOR_POLL_MS));
    }
}

void satellite_start(void) {
    if (xTaskCreatePinnedToCore(supervisor_task, "satellite", 6144, NULL, 5, NULL, 0) != pdPASS) {
        ESP_LOGE(TAG, "could not start the supervisor task");
    }
}
