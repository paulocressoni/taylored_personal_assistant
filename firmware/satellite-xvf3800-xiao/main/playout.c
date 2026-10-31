#include "playout.h"

#include <string.h>

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_memory_utils.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#include "audio_i2s.h"

static const char *TAG = "playout";

/* The server synthesises a whole answer and pushes it far faster than it plays
 * -- measured at roughly five times real time -- so the ring has to hold
 * almost the entire answer at once. Thirty seconds covers an answer of up to
 * about thirty-eight seconds, which is longer than any reply worth listening
 * to; the previous ten-second ring silently truncated a seventeen-second
 * answer. It costs 960 KB of PSRAM, of which this chip has 8 MB. */
#define PLAYOUT_SECONDS 30

/* If PSRAM is unavailable the device still has to work: two seconds in internal
 * RAM is enough for one sentence and is far better than no playback at all. */
#define PLAYOUT_FALLBACK_SECONDS 2

/* The server synthesises speech sentence by sentence, so it delivers audio in
 * bursts with pauses between them. If playback starts on the first burst it
 * catches up with the tail of that burst and falls silent until the next one
 * arrives -- an audible hole in the middle of a sentence. Holding back until
 * this much has accumulated rides through those pauses at the cost of a fixed
 * start-up delay. */
#define PLAYOUT_PREBUFFER_MS 350
#define PLAYOUT_PREBUFFER_SAMPLES ((size_t)AUDIO_SAMPLE_RATE * PLAYOUT_PREBUFFER_MS / 1000)

static int16_t *s_buf;
static size_t s_cap;
static size_t s_head;
static size_t s_tail;
static SemaphoreHandle_t s_lock;
static uint32_t s_overruns;

/* False while the ring is filling towards the prebuffer, and again after it has
 * run dry. Guarded by s_lock. */
static bool s_armed;

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
             * stays contiguous rather than jumping. Report it the moment it
             * happens -- the periodic counter can be a minute late, and by then
             * the listener has already heard speech with holes in it. */
            if (s_overruns == 0) {
                ESP_LOGW(TAG, "ring full: the answer is longer than the ring, "
                              "the rest of it will be lost");
            }
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

    xSemaphoreTake(s_lock, portMAX_DELAY);
    if (!s_armed && (s_head + s_cap - s_tail) % s_cap >= PLAYOUT_PREBUFFER_SAMPLES) {
        s_armed = true;
    }
    if (s_armed) {
        while (read < count && s_tail != s_head) {
            out[read++] = s_buf[s_tail];
            s_tail = (s_tail + 1) % s_cap;
        }
        if (read < count) {
            /* Caught up with the server mid-answer. Re-arm so playback resumes
             * on a full prebuffer instead of trickling frame by frame. */
            s_armed = false;
        }
    }
    xSemaphoreGive(s_lock);

    if (read < count) {
        /* Silence, not a stall: the clock must keep running. Whether this was
         * a fault is not knowable here -- the end of an answer looks exactly
         * like a gap in the middle of one -- so the caller judges it. */
        memset(out + read, 0, (count - read) * sizeof(int16_t));
    }
    return read;
}

bool playout_is_playing(void) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    const bool armed = s_armed;
    xSemaphoreGive(s_lock);
    return armed;
}

void playout_flush(void) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_head = 0;
    s_tail = 0;
    s_armed = false;
    xSemaphoreGive(s_lock);
}

size_t playout_level(void) {
    xSemaphoreTake(s_lock, portMAX_DELAY);
    const size_t level = (s_head + s_cap - s_tail) % s_cap;
    xSemaphoreGive(s_lock);
    return level;
}

void playout_log_stats(void) {
    if (s_overruns > 0) {
        ESP_LOGW(TAG, "overruns=%u (answer truncated: the ring was full)", (unsigned)s_overruns);
    }
    s_overruns = 0;
}
