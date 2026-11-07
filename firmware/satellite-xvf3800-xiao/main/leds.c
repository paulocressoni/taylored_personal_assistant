#include "leds.h"

#include "esp_log.h"

#include "xvf_control.h"

static const char *TAG = "leds";

/* Effect ids understood by the XVF3800's LED engine. Only the two the firmware
 * uses are named.
 *
 * The breath effect (id 1) is deliberately not used: at this brightness it is
 * dark for most of its cycle, and in practice a breathing state reads as "the
 * ring is off". A steady colour is unambiguous. Re-adding motion means first
 * raising LED_BRIGHTNESS and setting LED_SPEED explicitly, then confirming it
 * by eye. */
#define LED_EFFECT_OFF 0
#define LED_EFFECT_SINGLE 3

/* Brightness is global; 48 of 255 is bright enough to read in a lit room
 * without washing out the colours on the ring. */
#define LED_BRIGHTNESS 48

static led_state_t s_current;
static bool s_initialized;

/* Every command is a separate I2C transaction, and a failure used to be
 * discarded: the ring then stayed in whatever state the last successful write
 * left it, with nothing in the log to explain it. */
static void expect(esp_err_t err, const char *what) {
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "%s write failed: %s", what, esp_err_to_name(err));
    }
}

void leds_init(void) {
    s_initialized = true;
    s_current = LED_STATE_IDLE;

    /* The ring runs a rainbow, then a direction-of-arrival animation, unless
     * the host takes over. Selecting an effect explicitly stops both. */
    expect(xvf_set_led_effect(LED_EFFECT_SINGLE), "effect");
    expect(xvf_set_led_brightness(LED_BRIGHTNESS), "brightness");
    expect(xvf_set_led_color(0x0000FF), "colour");
    ESP_LOGI(TAG, "state -> %d (init)", (int)LED_STATE_IDLE);
}

void leds_set(led_state_t state) {
    if (s_initialized && state == s_current) {
        return;
    }
    s_initialized = true;
    s_current = state;

    /* Every state below ends with the same brightness write, so the effect is
     * only skipped for "off", which has no colour to show. */
    bool has_colour = true;

    switch (state) {
        case LED_STATE_OFF:
            expect(xvf_set_led_effect(LED_EFFECT_OFF), "effect");
            has_colour = false;
            break;
        case LED_STATE_IDLE:
            expect(xvf_set_led_effect(LED_EFFECT_SINGLE), "effect");
            expect(xvf_set_led_color(0x0000FF), "colour");
            break;
        case LED_STATE_LISTENING:
            /* Steady rather than breathing: "the session is ready" is a state,
             * not an animation, and a steady colour is unmistakable across a
             * room while a slow pulse at low brightness is not. */
            expect(xvf_set_led_effect(LED_EFFECT_SINGLE), "effect");
            expect(xvf_set_led_color(0x00FF00), "colour");
            break;
        case LED_STATE_THINKING:
            expect(xvf_set_led_effect(LED_EFFECT_SINGLE), "effect");
            expect(xvf_set_led_color(0xFF8000), "colour");
            break;
        case LED_STATE_SPEAKING:
            expect(xvf_set_led_effect(LED_EFFECT_SINGLE), "effect");
            expect(xvf_set_led_color(0x00FFFF), "colour");
            break;
        case LED_STATE_ERROR:
            /* Steady for the same reason as thinking: a fault the user cannot
             * see is a fault that costs an hour of debugging. */
            expect(xvf_set_led_effect(LED_EFFECT_SINGLE), "effect");
            expect(xvf_set_led_color(0xFF0000), "colour");
            break;
        case LED_STATE_MUTED:
            expect(xvf_set_led_effect(LED_EFFECT_SINGLE), "effect");
            expect(xvf_set_led_color(0xFF0000), "colour");
            break;
        default:
            ESP_LOGW(TAG, "unknown led state %d", (int)state);
            return;
    }

    if (has_colour) {
        expect(xvf_set_led_brightness(LED_BRIGHTNESS), "brightness");
    }
    ESP_LOGI(TAG, "state -> %d", (int)state);
}
