/*
 * app_main.c — the entry point of the satellite firmware.
 *
 * ESP-IDF calls app_main() once, after the chip has booted and the FreeRTOS
 * scheduler is running. At this step the firmware also opens the I2S audio
 * port and measures what the microphones deliver.
 */

#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include "audio_i2s.h"
#include "leds.h"
#include "wifi.h"
#include "xvf_control.h"

#include <stdint.h>

#include "esp_timer.h"

/* Every log line is prefixed with this tag, so you can tell which module
 * printed it. Each file will have its own tag. */
static const char *TAG = "sat";

/* The button is polled 50 times a second: an order of magnitude faster than
 * the shortest press a person makes, so no press can slip between samples. */
#define MUTE_POLL_MS 20

/* Three consecutive identical reads (60 ms) before a level is believed. */
#define MUTE_STABLE_POLLS 3

/* Independent of the poll rate: the heartbeat stays at one line every 5 s. */
#define HEARTBEAT_MS 5000

/* One peak reading per second: 50 frames of 20 ms. */
#define PEAK_FRAMES_PER_LOG 50

/*
 * Capture runs in its own task because an I2S read blocks until the DMA buffer
 * has data. Sharing the main loop with the mute poll would either delay the
 * audio or make the button miss presses, and the audio deadline is the one
 * that cannot slip.
 */
static void capture_task(void *arg) {
    int16_t left[AUDIO_FRAME_SAMPLES];
    int16_t right[AUDIO_FRAME_SAMPLES];
    const int16_t silence[AUDIO_FRAME_SAMPLES] = {0};

    int peak_left = 0;
    int peak_right = 0;
    int frames = 0;
    int errors = 0;

    for (;;) {
        /* The speaker path is idle until playback exists; writing silence
         * keeps the shared clock fed and gives the DSP a far-end reference. */
        audio_write(silence, AUDIO_FRAME_SAMPLES, pdMS_TO_TICKS(100));

        if (audio_read(left, right, AUDIO_FRAME_SAMPLES, pdMS_TO_TICKS(100)) != ESP_OK) {
            errors++;
        } else {
            for (size_t i = 0; i < AUDIO_FRAME_SAMPLES; i++) {
                const int l = left[i] < 0 ? -left[i] : left[i];
                const int r = right[i] < 0 ? -right[i] : right[i];
                if (l > peak_left) {
                    peak_left = l;
                }
                if (r > peak_right) {
                    peak_right = r;
                }
            }
        }

        if (++frames >= PEAK_FRAMES_PER_LOG) {
            /* Both channels carry the same ASR beam, so the two numbers should
             * track each other; a large difference means the slots are read or
             * clocked wrongly. */
            ESP_LOGI(TAG, "mic peak L=%d R=%d (%d frames, %d errors)", peak_left, peak_right,
                     frames, errors);
            peak_left = 0;
            peak_right = 0;
            frames = 0;
            errors = 0;
        }
    }
}

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

    /* The audio path depends on the XVF3800, so talk to it before the radio.
     * A failure is reported, not fatal: the log is more useful than a reboot
     * loop, and the reasons are almost always wiring or the wrong firmware. */
    err = xvf_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "XVF3800 unavailable: %s", esp_err_to_name(err));
    }
    xvf_log_identity();

    /* Headphones are on the 3.5 mm jack for the bench, so the speaker
     * amplifier stays off; see xvf_set_amp_enabled for why that also matters
     * to the microphones. */
    if (xvf_set_amp_enabled(false) != ESP_OK) {
        ESP_LOGW(TAG, "could not configure the amplifier");
    }
    /* Without this the mute button mutes the XVF in hardware and the host
     * never learns it was pressed. */
    if (xvf_disable_mute_function() != ESP_OK) {
        ESP_LOGW(TAG, "could not take over mute control");
    }

    leds_init();
    leds_set(LED_STATE_IDLE);

    err = audio_init();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2S unavailable: %s", esp_err_to_name(err));
    } else {
        xTaskCreatePinnedToCore(capture_task, "capture", 4096, NULL, 10, NULL, 1);
    }

    err = wifi_start();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "wifi_start failed: %s", esp_err_to_name(err));
    }

    /* Mute is a latch: one press mutes, the next unmutes. That is what the
     * hardware button on a voice assistant means, and it is what this
     * firmware has to implement itself now that MUTE_FUNCTION_ENABLE=0 has
     * made the button a pure event. */
    bool muted = false;

    /* Debounce state. A mechanical button bounces for a few milliseconds, and
     * the XVF3800's own reading can be stale for the first moments after
     * boot, so a level is only accepted after it has held for several polls. */
    bool observed = false;
    bool stable = false;
    int stable_polls = 0;

    int64_t next_heartbeat_ms = 0;

    for (;;) {
        bool pressed = false;
        if (xvf_read_mute_button(&pressed) == ESP_OK) {
            if (pressed == observed) {
                if (stable_polls < MUTE_STABLE_POLLS) {
                    stable_polls++;
                }
            } else {
                observed = pressed;
                stable_polls = 1;
            }

            /* Accept a transition only once the new level has survived
             * MUTE_STABLE_POLLS consecutive reads. stable is then updated, so
             * the condition cannot fire twice for the same edge. */
            if (stable_polls >= MUTE_STABLE_POLLS && stable != observed) {
                stable = observed;
                if (stable) {
                    muted = !muted;
                    ESP_LOGI(TAG, "mute %s", muted ? "on" : "off");
                    leds_set(muted ? LED_STATE_MUTED : LED_STATE_IDLE);
                }
            }
        }

        /* The heartbeat is a separate concern from the button, so it counts
         * elapsed time instead of sharing the poll interval. */
        const int64_t now_ms = esp_timer_get_time() / 1000;
        if (now_ms >= next_heartbeat_ms) {
            next_heartbeat_ms = now_ms + HEARTBEAT_MS;
            ESP_LOGI(TAG, "heartbeat: %s%s", wifi_is_connected() ? "online" : "offline",
                     muted ? " (muted)" : "");
        }

        vTaskDelay(pdMS_TO_TICKS(MUTE_POLL_MS));
    }
}
