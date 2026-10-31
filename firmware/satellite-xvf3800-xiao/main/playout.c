#include "playout.h"

#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_memory_utils.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#include "audio_i2s.h"

static const char *TAG = "playout";

/* Ten seconds. A spoken answer is typically two to six, and the server sends it
 * faster than it plays, so the ring absorbs the burst instead of the speaker
 * stuttering. */
#define PLAYOUT_SECONDS 10

/* If PSRAM is unavailable the device still has to work: two seconds in internal
 * RAM is enough for one sentence and is far better than no playback at all. */
#define PLAYOUT_FALLBACK_SECONDS 2

static int16_t *s_buf;
static size_t s_cap;
static size_t s_head;
static size_t s_tail;
static SemaphoreHandle_t s_lock;
static uint32_t s_underruns;
static uint32_t s_overruns;

esp_err_t playout_init(void) {
    s_cap = (size_t)AUDIO_SAMPLE_RATE * PLAYOUT_SECONDS;
    s_buf = heap_caps_malloc(s_cap * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (s_buf == NULL) {
        s_cap = (size_t)AUDIO_SAMPLE_RATE * PLAYOUT_FALLBACK_SECONDS;
        s_buf = heap_caps_malloc(s_cap * sizeof(int16_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    }
    if (s_buf == NULL) {
        ESP_LOGE(TAG, "no memory for the playout ring");
        return ESP_ERR_NO_MEM;
    }

    s_lock = xSemaphoreCreateMutex();
    if (s_lock == NULL) {
        heap_caps_free(s_buf);
        s_buf = NULL;
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "ring: %u samples (%u s) in %s", (unsigned)s_cap,
             (unsigned)(s_cap / AUDIO_SAMPLE_RATE),
             esp_ptr_internal(s_buf) ? "internal RAM" : "PSRAM");
    return ESP_OK;
}

size_t playout_write(const int16_t *samples, size_t count) {
    size_t written = 0;

    xSemaphoreTake(s_lock, portMAX_DELAY);
    while (written < count) {
        const size_t next = (s_head + 1) % s_cap;
        if (next == s_tail) {
            /* Full: keep the oldest audio and drop the newest, so playback
             * stays contiguous rather than jumping. */
            s_overruns += (uint32_t)(count - written);
            break;
        }
        s_buf[s_head] = samples[written];
        s_head = next;
        written++;
    }
    xSemaphoreGive(s_lock);
    return written;
}

size_t playout_read(int16_t *out, size_t count) {
    size_t read = 0;
    bool had_data;

    xSemaphoreTake(s_lock, portMAX_DELAY);
    had_data = s_head != s_tail;
    while (read < count && s_tail != s_head) {
        out[read++] = s_buf[s_tail];
        s_tail = (s_tail + 1) % s_cap;
    }
    xSemaphoreGive(s_lock);

    if (read < count) {
        memset(out + read, 0, (count - read) * sizeof(int16_t));
        /* Counted only when audio was actually interrupted: an idle ring is
         * silence by design, not an underrun. */
        if (had_data) {
            s_underruns++;
        }
    }
    return read;
}

void playout_flush(void) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_head = 0;
    s_tail = 0;
    xSemaphoreGive(s_lock);
}

size_t playout_level(void) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    const size_t level = (s_head + s_cap - s_tail) % s_cap;
    xSemaphoreGive(s_lock);
    return level;
}

void playout_log_stats(void) {
    if (s_underruns > 0 || s_overruns > 0) {
        ESP_LOGW(TAG, "underruns=%u overruns=%u", (unsigned)s_underruns, (unsigned)s_overruns);
    }
    s_underruns = 0;
    s_overruns = 0;
}
