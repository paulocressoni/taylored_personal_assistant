#include "xvf_control.h"

#include <string.h>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "xvf";

/* Bus wiring, fixed by the carrier board. */
#define XVF_I2C_PORT I2C_NUM_0
#define XVF_I2C_SDA GPIO_NUM_5
#define XVF_I2C_SCL GPIO_NUM_6
#define XVF_I2C_ADDR 0x2C
#define XVF_TIMEOUT_MS 100

/* The XVF3800 groups its commands by "resource id". */
#define XVF_RESID_APP 48
#define XVF_RESID_GPO 20
#define XVF_RESID_IO_CONFIG 36

/* A read is a write with this bit set in the command byte. */
#define XVF_CMD_READ_BIT 0x80

/* Command ids within each resource. */
#define XVF_CMD_VERSION 0
#define XVF_CMD_BUILD 1
#define XVF_CMD_GPO_WRITE 1
#define XVF_CMD_LED_EFFECT 12
#define XVF_CMD_LED_BRIGHTNESS 13
#define XVF_CMD_LED_COLOR 16
#define XVF_CMD_MUTE_FUNCTION 8
#define XVF_CMD_GPI_VALUES 6

#define XVF_GPO_AMP_ENABLE 31
#define XVF_MAX_PAYLOAD 16
#define XVF_MAX_READ 64

static i2c_master_bus_handle_t s_bus;
static i2c_master_dev_handle_t s_dev;
static bool s_ready;

/*
 * Device control framing: [resid, cmd, payload_len, payload...]. A read sends
 * [resid, cmd | 0x80, read_len + 1] and gets [status, data...] back; a non-zero
 * status means the device rejected the command.
 */
static esp_err_t xvf_write(uint8_t resid, uint8_t cmd, const uint8_t *payload, size_t len) {
    if (!s_ready) {
        return ESP_ERR_INVALID_STATE;
    }
    uint8_t frame[3 + XVF_MAX_PAYLOAD];
    if (len > XVF_MAX_PAYLOAD) {
        return ESP_ERR_INVALID_SIZE;
    }
    frame[0] = resid;
    frame[1] = cmd;
    frame[2] = (uint8_t)len;
    if (len > 0) {
        memcpy(&frame[3], payload, len);
    }
    return i2c_master_transmit(s_dev, frame, 3 + len, XVF_TIMEOUT_MS);
}

static esp_err_t xvf_read(uint8_t resid, uint8_t cmd, uint8_t *out, size_t len) {
    if (!s_ready) {
        return ESP_ERR_INVALID_STATE;
    }
    if (len > XVF_MAX_READ) {
        return ESP_ERR_INVALID_SIZE;
    }
    const uint8_t request[3] = {resid, (uint8_t)(cmd | XVF_CMD_READ_BIT), (uint8_t)(len + 1)};
    uint8_t response[1 + XVF_MAX_READ];

    esp_err_t err = i2c_master_transmit_receive(s_dev, request, sizeof(request), response, len + 1,
                                                XVF_TIMEOUT_MS);
    if (err != ESP_OK) {
        return err;
    }
    if (response[0] != 0) {
        ESP_LOGW(TAG, "resid %u cmd %u rejected (status 0x%02x)", resid, cmd, response[0]);
        return ESP_FAIL;
    }
    memcpy(out, &response[1], len);
    return ESP_OK;
}

esp_err_t xvf_init(void) {
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = XVF_I2C_PORT,
        .sda_io_num = XVF_I2C_SDA,
        .scl_io_num = XVF_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_RETURN_ON_ERROR(i2c_new_master_bus(&bus_cfg, &s_bus), TAG, "i2c bus");

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = XVF_I2C_ADDR,
        .scl_speed_hz = 100000,
    };
    ESP_RETURN_ON_ERROR(i2c_master_bus_add_device(s_bus, &dev_cfg, &s_dev), TAG, "i2c device");
    s_ready = true;

    /* The XVF3800 spends about two seconds booting; poll rather than assume. */
    uint8_t version[3] = {0};
    for (int attempt = 0; attempt < 20; attempt++) {
        if (xvf_read(XVF_RESID_APP, XVF_CMD_VERSION, version, sizeof(version)) == ESP_OK) {
            ESP_LOGI(TAG, "firmware %u.%u.%u on i2c 0x%02x", version[0], version[1], version[2],
                     XVF_I2C_ADDR);
            return ESP_OK;
        }
        vTaskDelay(pdMS_TO_TICKS(250));
    }

    s_ready = false;
    ESP_LOGE(TAG, "no answer at i2c 0x%02x", XVF_I2C_ADDR);
    return ESP_ERR_TIMEOUT;
}

void xvf_log_identity(void) {
    uint8_t version[3] = {0};
    uint8_t build[51] = {0};

    if (xvf_read(XVF_RESID_APP, XVF_CMD_VERSION, version, sizeof(version)) != ESP_OK ||
        xvf_read(XVF_RESID_APP, XVF_CMD_BUILD, build, sizeof(build) - 1) != ESP_OK) {
        ESP_LOGW(TAG, "could not read the firmware identity");
        return;
    }
    build[sizeof(build) - 1] = '\0';
    ESP_LOGI(TAG, "firmware %u.%u.%u, build \"%s\"", version[0], version[1], version[2],
             (const char *)build);
}

esp_err_t xvf_set_led_effect(uint8_t effect) {
    return xvf_write(XVF_RESID_GPO, XVF_CMD_LED_EFFECT, &effect, 1);
}

esp_err_t xvf_set_led_brightness(uint8_t brightness) {
    return xvf_write(XVF_RESID_GPO, XVF_CMD_LED_BRIGHTNESS, &brightness, 1);
}

esp_err_t xvf_set_led_color(uint32_t rgb) {
    /* The device takes the colour as four bytes, least significant first. */
    const uint8_t payload[4] = {
        (uint8_t)(rgb & 0xff),
        (uint8_t)((rgb >> 8) & 0xff),
        (uint8_t)((rgb >> 16) & 0xff),
        (uint8_t)((rgb >> 24) & 0xff),
    };
    return xvf_write(XVF_RESID_GPO, XVF_CMD_LED_COLOR, payload, sizeof(payload));
}

esp_err_t xvf_set_amp_enabled(bool enabled) {
    /* The amplifier enable is active low: 0 turns it on. */
    const uint8_t payload[2] = {XVF_GPO_AMP_ENABLE, (uint8_t)(enabled ? 0 : 1)};
    return xvf_write(XVF_RESID_GPO, XVF_CMD_GPO_WRITE, payload, sizeof(payload));
}

esp_err_t xvf_disable_mute_function(void) {
    const uint8_t disabled = 0;
    return xvf_write(XVF_RESID_IO_CONFIG, XVF_CMD_MUTE_FUNCTION, &disabled, 1);
}

static uint32_t s_last_gpi_raw;
static bool s_last_gpi_valid;

esp_err_t xvf_read_mute_button(bool *pressed) {
    uint8_t raw[4] = {0};
    esp_err_t err = xvf_read(XVF_RESID_IO_CONFIG, XVF_CMD_GPI_VALUES, raw, sizeof(raw));
    if (err != ESP_OK) {
        return err;
    }

    const uint32_t value = raw[0] | (raw[1] << 8) | (raw[2] << 16) | ((uint32_t)raw[3] << 24);

    /* Log the raw bytes on every change. The byte order and field width of
     * this register are not documented in a form that can be trusted, so the
     * log is the source of truth while the layout is being confirmed. */
    if (!s_last_gpi_valid || value != s_last_gpi_raw) {
        s_last_gpi_valid = true;
        s_last_gpi_raw = value;
        ESP_LOGI(TAG, "mute gpio: %02x %02x %02x %02x -> 0x%08x", raw[0], raw[1], raw[2], raw[3],
                 (unsigned)value);
    }

    /* Bit 0 reads 1 while the button is released and 0 while pressed. */
    *pressed = (value & 0x01) == 0;
    return ESP_OK;
}
