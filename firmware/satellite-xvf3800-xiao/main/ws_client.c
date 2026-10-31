#include "ws_client.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_websocket_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#include "audio_i2s.h"
#include "satellite.h"

static const char *TAG = "ws";

/* Frames the server sends can exceed one WebSocket frame, in which case the
 * client delivers them in pieces. This buffer reassembles one text payload. */
#define WS_TEXT_MAX 1024

static SemaphoreHandle_t s_lock;
static esp_websocket_client_handle_t s_client;
static char s_text[WS_TEXT_MAX];
static size_t s_text_len;

/* Percent-encode anything that is not unreserved in a URI (RFC 3986), so an
 * API key containing '+', '/' or '=' survives the query string. */
static bool percent_encode(const char *in, char *out, size_t cap) {
    static const char HEX[] = "0123456789ABCDEF";
    size_t j = 0;

    for (size_t i = 0; in[i] != '\0'; i++) {
        const unsigned char c = (unsigned char)in[i];
        const bool unreserved = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                                (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' ||
                                c == '~';
        if (unreserved) {
            if (j + 1 >= cap) {
                return false;
            }
            out[j++] = (char)c;
        } else {
            if (j + 3 >= cap) {
                return false;
            }
            out[j++] = '%';
            out[j++] = HEX[c >> 4];
            out[j++] = HEX[c & 0x0f];
        }
    }
    out[j] = '\0';
    return true;
}

/* Assemble the full URI. The key travels as a query parameter because the
 * client does not offer a subprotocol header, which is what the browser uses. */
static bool build_uri(char *out, size_t cap) {
    if (CONFIG_SATELLITE_SERVER_URI[0] == '\0' || CONFIG_SATELLITE_API_KEY[0] == '\0') {
        ESP_LOGE(TAG, "server URI or API key is empty: run idf.py menuconfig");
        return false;
    }

    char key[3 * 128 + 1];
    if (!percent_encode(CONFIG_SATELLITE_API_KEY, key, sizeof(key))) {
        ESP_LOGE(TAG, "API key is too long to encode");
        return false;
    }

    const char *separator = strchr(CONFIG_SATELLITE_SERVER_URI, '?') == NULL ? "?" : "&";
    const int written =
        snprintf(out, cap, "%s%sapi_key=%s", CONFIG_SATELLITE_SERVER_URI, separator, key);
    return written > 0 && (size_t)written < cap;
}

/* The first frame of a session: which conversation, and the audio contract. */
static void send_start_frame(esp_websocket_client_handle_t client) {
    char device[64];
    char session[80];
    char frame[256];

    satellite_device_id(device, sizeof(device));
    snprintf(session, sizeof(session), "sat-%s", device);

    const int len = snprintf(frame, sizeof(frame),
                             "{\"session_id\":\"%s\",\"device_id\":\"%s\","
                             "\"output_sample_rate\":%d}",
                             session, device, AUDIO_SAMPLE_RATE);
    if (len <= 0 || (size_t)len >= sizeof(frame)) {
        ESP_LOGE(TAG, "start frame does not fit");
        return;
    }

    if (esp_websocket_client_send_text(client, frame, len, pdMS_TO_TICKS(2000)) < 0) {
        ESP_LOGE(TAG, "could not send the start frame");
        return;
    }
    ESP_LOGI(TAG, "session %s -> %s", session, device);
}

static void handle_close(uint16_t code) {
    /* 1008 is policy: a wrong key, or voice switched off on the server.
     * Retrying will not fix it, so it is worth saying so plainly. */
    switch (code) {
        case 1008:
            ESP_LOGE(TAG, "server refused the session (1008): check the API key and VOICE_ENABLED");
            break;
        case 1003:
            ESP_LOGE(TAG, "server rejected the start frame (1003)");
            break;
        case 1013:
            ESP_LOGW(TAG, "server is busy or rate limiting (1013)");
            break;
        default:
            ESP_LOGW(TAG, "server closed the socket (code %u)", code);
            break;
    }
}

static void ws_event_handler(void *arg, esp_event_base_t base, int32_t id, void *event_data) {
    esp_websocket_client_handle_t client = (esp_websocket_client_handle_t)arg;
    const esp_websocket_event_data_t *data = (const esp_websocket_event_data_t *)event_data;

    switch (id) {
        case WEBSOCKET_EVENT_CONNECTED:
            ESP_LOGI(TAG, "connected");
            satellite_on_ws_connected();
            send_start_frame(client);
            break;

        case WEBSOCKET_EVENT_DATA:
            if (data == NULL) {
                break;
            }
            /* 0x01 is a text frame; 0x02 binary, which the next step uses. */
            if (data->op_code == 0x01) {
                if (data->payload_offset == 0) {
                    s_text_len = 0;
                }
                const size_t room = sizeof(s_text) - s_text_len - 1;
                const size_t copy = (size_t)data->data_len < room ? (size_t)data->data_len : room;
                memcpy(s_text + s_text_len, data->data_ptr, copy);
                s_text_len += copy;

                if (data->payload_offset + data->data_len >= data->payload_len) {
                    s_text[s_text_len] = '\0';
                    satellite_on_ws_text(s_text, s_text_len);
                    s_text_len = 0;
                }
            }
            break;

        case WEBSOCKET_EVENT_CLOSED:
            /* A CLOSE frame arrives as this event, not as a DATA frame, but the
             * event data still carries the RFC 6455 status code. */
            if (data != NULL) {
                handle_close((uint16_t)data->close_status_code);
            }
            satellite_on_ws_down();
            break;

        case WEBSOCKET_EVENT_ERROR:
            /* A failed HTTP upgrade is not a dropped session: the server never
             * spoke WebSocket at all. The usual causes are a wrong host, a
             * wrong port, or a key the proxy rejected. */
            if (data != NULL && data->error_handle.error_type == WEBSOCKET_ERROR_TYPE_HANDSHAKE) {
                ESP_LOGE(TAG, "handshake failed (HTTP %d): check the URI and the API key",
                         data->error_handle.esp_ws_handshake_status_code);
            }
            satellite_on_ws_down();
            break;

        case WEBSOCKET_EVENT_DISCONNECTED:
            satellite_on_ws_down();
            break;

        default:
            break;
    }
}

esp_err_t ws_start(void) {
    if (s_lock == NULL) {
        s_lock = xSemaphoreCreateMutex();
        if (s_lock == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    if (xSemaphoreTake(s_lock, pdMS_TO_TICKS(1000)) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }

    esp_err_t err = ESP_OK;
    if (s_client != NULL) {
        goto out;
    }

    char uri[512];
    if (!build_uri(uri, sizeof(uri))) {
        err = ESP_ERR_INVALID_ARG;
        goto out;
    }

    /* The URI is logged without the key, which must never appear in a log. */
    ESP_LOGI(TAG, "connecting to %s", CONFIG_SATELLITE_SERVER_URI);

    esp_websocket_client_config_t cfg = {
        .uri = uri,
        /* Large enough for one server frame: the PCM chunks can be a few KB. */
        .buffer_size = 4096,
        .task_stack = 6144,
        /* The supervisor decides when to retry, so the client must not do it
         * behind our back: two reconnect loops would race. */
        .disable_auto_reconnect = true,
        .network_timeout_ms = 10000,
    };

    s_client = esp_websocket_client_init(&cfg);
    if (s_client == NULL) {
        err = ESP_ERR_NO_MEM;
        goto out;
    }

    err = esp_websocket_register_events(s_client, WEBSOCKET_EVENT_ANY, ws_event_handler, s_client);
    if (err == ESP_OK) {
        err = esp_websocket_client_start(s_client);
    }
    if (err != ESP_OK) {
        esp_websocket_client_destroy(s_client);
        s_client = NULL;
    }

out:
    xSemaphoreGive(s_lock);
    return err;
}

void ws_stop(void) {
    if (s_lock == NULL) {
        return;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    if (s_client != NULL) {
        esp_websocket_client_stop(s_client);
        esp_websocket_client_destroy(s_client);
        s_client = NULL;
    }
    s_text_len = 0;
    xSemaphoreGive(s_lock);
}

bool ws_is_connected(void) {
    if (s_lock == NULL || xSemaphoreTake(s_lock, pdMS_TO_TICKS(50)) != pdTRUE) {
        return false;
    }
    const bool connected = s_client != NULL && esp_websocket_client_is_connected(s_client);
    xSemaphoreGive(s_lock);
    return connected;
}

bool ws_send_text(const char *text) {
    if (s_lock == NULL || xSemaphoreTake(s_lock, pdMS_TO_TICKS(50)) != pdTRUE) {
        return false;
    }

    bool sent = false;
    if (s_client != NULL && esp_websocket_client_is_connected(s_client)) {
        const int len = (int)strlen(text);
        sent = esp_websocket_client_send_text(s_client, text, len, pdMS_TO_TICKS(1000)) == len;
    }
    xSemaphoreGive(s_lock);
    return sent;
}
