#include "audio_i2s.h"

#include <string.h>

#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "esp_check.h"
#include "esp_log.h"

static const char *TAG = "i2s";

/* Carrier-board wiring. The XVF3800 owns its own audio clock, so MCLK is not
 * routed; only the bit clock and the word select are shared. */
#define AUDIO_I2S_BCLK GPIO_NUM_8
#define AUDIO_I2S_WS GPIO_NUM_7
#define AUDIO_I2S_DIN GPIO_NUM_43
#define AUDIO_I2S_DOUT GPIO_NUM_44

/* The XVF3800 delivers two 32-bit slots whose DSP result sits in the top 16
 * bits, so shifting right by 16 yields the signed 16-bit sample the server
 * expects. */
#define SLOT_SHIFT 16

static i2s_chan_handle_t s_tx;
static i2s_chan_handle_t s_rx;

/* One frame of raw slots per direction: stereo, 32 bits per slot. */
static uint32_t s_rx_raw[AUDIO_FRAME_SAMPLES * 2];
static uint32_t s_tx_raw[AUDIO_FRAME_SAMPLES * 2];

esp_err_t audio_init(void) {
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_AUTO, I2S_ROLE_MASTER);

    /* One DMA descriptor per 20 ms frame, four of them: the driver fills
     * descriptor N+1 while the application consumes descriptor N, which
     * leaves 80 ms of slack for the scheduler. */
    chan_cfg.dma_desc_num = 4;
    chan_cfg.dma_frame_num = AUDIO_FRAME_SAMPLES;

    /* When the transmit queue runs dry the peripheral sends zeros instead of
     * repeating the last buffer, so an idle output is silent rather than a
     * stuck tone. */
    chan_cfg.auto_clear = true;

    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, &s_tx, &s_rx), TAG, "new channel");

    /* Requesting both handles puts the port in full duplex: TX and RX share
     * BCLK and WS, so the microphone and the speaker run on one clock. Both
     * channels must be initialised with the same clock and slot settings. */
    const i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(AUDIO_SAMPLE_RATE),
        .slot_cfg =
            I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg =
            {
                .mclk = I2S_GPIO_UNUSED,
                .bclk = AUDIO_I2S_BCLK,
                .ws = AUDIO_I2S_WS,
                .dout = AUDIO_I2S_DOUT,
                .din = AUDIO_I2S_DIN,
                .invert_flags =
                    {
                        .mclk_inv = false,
                        .bclk_inv = false,
                        .ws_inv = false,
                    },
            },
    };

    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_tx, &std_cfg), TAG, "tx init");
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_rx, &std_cfg), TAG, "rx init");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(s_tx), TAG, "tx enable");
    ESP_RETURN_ON_ERROR(i2s_channel_enable(s_rx), TAG, "rx enable");

    ESP_LOGI(TAG, "%d Hz, 32-bit stereo, ESP32 is master", AUDIO_SAMPLE_RATE);
    return ESP_OK;
}

esp_err_t audio_read(int16_t *left, int16_t *right, size_t count, TickType_t timeout) {
    if (count > AUDIO_FRAME_SAMPLES) {
        count = AUDIO_FRAME_SAMPLES;
    }

    size_t bytes = 0;
    const esp_err_t err =
        i2s_channel_read(s_rx, s_rx_raw, count * 2 * sizeof(uint32_t), &bytes, timeout);
    if (err != ESP_OK) {
        memset(left, 0, count * sizeof(int16_t));
        if (right != NULL) {
            memset(right, 0, count * sizeof(int16_t));
        }
        return err;
    }

    const size_t frames = bytes / (2 * sizeof(uint32_t));
    for (size_t i = 0; i < frames; i++) {
        left[i] = (int16_t)(s_rx_raw[i * 2] >> SLOT_SHIFT);
        if (right != NULL) {
            right[i] = (int16_t)(s_rx_raw[i * 2 + 1] >> SLOT_SHIFT);
        }
    }

    /* A short read means the DMA starved; silence is the safe filler. */
    if (frames < count) {
        memset(left + frames, 0, (count - frames) * sizeof(int16_t));
        if (right != NULL) {
            memset(right + frames, 0, (count - frames) * sizeof(int16_t));
        }
    }
    return ESP_OK;
}

esp_err_t audio_write(const int16_t *samples, size_t count, TickType_t timeout) {
    if (count > AUDIO_FRAME_SAMPLES) {
        count = AUDIO_FRAME_SAMPLES;
    }

    for (size_t i = 0; i < count; i++) {
        const uint32_t slot = (uint32_t)((int32_t)samples[i] << SLOT_SHIFT);
        s_tx_raw[i * 2] = slot;
        s_tx_raw[i * 2 + 1] = slot;
    }

    size_t written = 0;
    return i2s_channel_write(s_tx, s_tx_raw, count * 2 * sizeof(uint32_t), &written, timeout);
}
