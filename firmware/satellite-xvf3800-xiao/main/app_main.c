/*
 * app_main.c — the entry point of the satellite firmware.
 *
 * ESP-IDF calls app_main() once, after the chip has booted and the FreeRTOS
 * scheduler is running. At this step the firmware opens the I2S audio port,
 * measures what the microphones deliver, and opens the voice socket so the
 * session handshake can be verified before audio is streamed over it.
 */

#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include "audio_i2s.h"
#include "leds.h"
#include "playout.h"
#include "satellite.h"
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

/* One counter line per second: 50 frames of 20 ms. */
#define AUDIO_FRAMES_PER_LOG 50

/* Frames read and discarded after starting, so the first measurement does not
 * include data captured before the XVF3800's clocks settled. */
#define STARTUP_DRAIN_FRAMES 5

/*
 * Frame buffers live in .bss, not on the task stack: two 640-byte arrays plus
 * the socket call do not fit comfortably in a modest FreeRTOS stack, and only
 * this task touches them.
 */
static int16_t s_frame_left[AUDIO_FRAME_SAMPLES];
static int16_t s_frame_right[AUDIO_FRAME_SAMPLES];
static int16_t s_frame_out[AUDIO_FRAME_SAMPLES];

/*
 * The uplink task owns the socket write path. Separating it from capture is
 * what makes the audio path immune to network latency: the capture task never
 * waits, and this task can block as long as the socket needs without ever
 * missing an I2S frame.
 */
static void uplink_task(void *arg) {
    int16_t frame[AUDIO_FRAME_SAMPLES];
    uint32_t sent = 0;
    uint32_t failed = 0;
    uint32_t frames = 0;

    for (;;) {
        const size_t got = satellite_take_mic(frame, AUDIO_FRAME_SAMPLES);
        if (got == 0) {
            /* Nothing captured yet: wait rather than spin. */
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }

        if (!satellite_is_ready()) {
            /* No session, so there is nobody to send to. Discard silently:
             * counting these as failures would drown the real ones. */
            continue;
        }

        if (satellite_send_audio(frame, got)) {
            sent++;
        } else {
            failed++;
        }

        if (++frames >= AUDIO_FRAMES_PER_LOG) {
            /* A failure here is a frame the server never heard: it shows up as
             * a hole in the transcript, not as an error anywhere else. */
            ESP_LOGI(TAG, "uplink sent=%u failed=%u", (unsigned)sent, (unsigned)failed);
            sent = 0;
            failed = 0;
            frames = 0;
        }
    }
}

/*
 * The playout task is the only writer on the transmit channel. It runs on its
 * own 20 ms cadence, driven by the I2S clock, and stays completely independent
 * of the network: the ring absorbs the server's bursts and this task drains it
 * at exactly the rate the speaker consumes. That decoupling is what stops
 * network jitter from becoming an audible stutter.
 */
static void playout_task(void *arg) {
    for (;;) {
        /* Always writes a full frame. An empty ring yields silence, so the
         * clock never stops and an idle speaker is quiet rather than clicking. */
        const bool was_playing = playout_is_playing();
        const size_t got = playout_read(s_frame_out, AUDIO_FRAME_SAMPLES);
        if (was_playing && got < AUDIO_FRAME_SAMPLES) {
            /* Only a ring that ran dry mid-answer is a fault; one that has not
             * started yet is just waiting for the prebuffer, and satellite.c
             * decides whether the answer is still in flight. */
            ESP_LOGW(TAG, "playout ran dry after %u of %u samples", (unsigned)got,
                     (unsigned)AUDIO_FRAME_SAMPLES);
            satellite_note_audio_shortfall();
        }
        audio_write(s_frame_out, AUDIO_FRAME_SAMPLES, pdMS_TO_TICKS(100));
    }
}

/*
 * Capture runs in its own task because an I2S read blocks until the DMA buffer
 * has data. Sharing the main loop with the mute poll would either delay the
 * audio or make the button miss presses, and the audio deadline is the one
 * that cannot slip.
 */
static void capture_task(void *arg) {
    uint32_t errors = 0;
    uint32_t frames = 0;

    /* The receive DMA can hold data from before the XVF3800's clocks settled,
     * which shows up as a full-scale burst in the first reading. Drain those
     * frames before sending anything. */
    for (int i = 0; i < STARTUP_DRAIN_FRAMES; i++) {
        audio_read(s_frame_left, s_frame_right, AUDIO_FRAME_SAMPLES, pdMS_TO_TICKS(100));
    }

    for (;;) {
        if (audio_read(s_frame_left, s_frame_right, AUDIO_FRAME_SAMPLES, pdMS_TO_TICKS(100)) !=
            ESP_OK) {
            errors++;
        } else {
            /* Handed over and forgotten: this task's only deadline is the next
             * frame, so it must never wait for the network. */
            satellite_queue_mic(s_frame_left, AUDIO_FRAME_SAMPLES);
        }

        if (++frames >= AUDIO_FRAMES_PER_LOG) {
            /* The stack figure is measured headroom, not an estimate. Send
             * failures are counted by the uplink task, which owns them. */
            ESP_LOGI(TAG, "capture errors=%u (stack free %u bytes)", (unsigned)errors,
                     (unsigned)uxTaskGetStackHighWaterMark(NULL));
            errors = 0;
            frames = 0;
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
        /* Playback is optional: without a ring the device is still a working
         * microphone, so a failure here is reported and not fatal. */
        if (playout_init() == ESP_OK) {
            if (xTaskCreatePinnedToCore(playout_task, "playout", 4096, NULL, 11, NULL, 1) !=
                pdPASS) {
                ESP_LOGE(TAG, "could not start the playout task");
            }
        }

        /* 8192 bytes is measured, not guessed: the high-water mark logged each
         * second reports the real margin. A create failure is reported rather
         * than ignored, because without this task the device is deaf. */
        if (xTaskCreatePinnedToCore(capture_task, "capture", 8192, NULL, 10, NULL, 1) != pdPASS) {
            ESP_LOGE(TAG, "could not start the capture task");
        }
    }

    err = wifi_start();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "wifi_start failed: %s", esp_err_to_name(err));
    }

    /* The uplink queue must exist before the capture task starts filling it. */
    if (satellite_init() == ESP_OK) {
        if (xTaskCreatePinnedToCore(uplink_task, "uplink", 4096, NULL, 9, NULL, 1) != pdPASS) {
            ESP_LOGE(TAG, "could not start the uplink task");
        }
    }

    /* The supervisor owns the socket: it waits for Wi-Fi, connects, retries
     * with backoff, and keeps the session alive between turns. */
    satellite_start();

    /* The mute latch lives in the satellite module, because it is the audio
     * path that has to honour it; this loop only drives the button and the
     * LED. */

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
                    const bool muted = satellite_toggle_mute();
                    ESP_LOGI(TAG, "mute %s", muted ? "on" : "off");
                    leds_set(muted ? LED_STATE_MUTED
                                   : (satellite_is_ready() ? LED_STATE_LISTENING : LED_STATE_IDLE));
                }
            }
        }

        /* The heartbeat is a separate concern from the button, so it counts
         * elapsed time instead of sharing the poll interval. */
        const int64_t now_ms = esp_timer_get_time() / 1000;
        if (now_ms >= next_heartbeat_ms) {
            next_heartbeat_ms = now_ms + HEARTBEAT_MS;
            ESP_LOGI(TAG, "heartbeat: %s%s%s", wifi_is_connected() ? "online" : "offline",
                     satellite_is_ready() ? " ready" : "", satellite_is_muted() ? " (muted)" : "");
        }

        vTaskDelay(pdMS_TO_TICKS(MUTE_POLL_MS));
    }
}
