#include "leds.h"

#include "esp_log.h"

#include "xvf_control.h"

static const char *TAG = "leds";

/* Effect ids understood by the XVF3800's LED engine. */
#define LED_EFFECT_OFF 0
#define LED_EFFECT_BREATH 1
#define LED_EFFECT_RAINBOW 2
#define LED_EFFECT_SINGLE 3
#define LED_EFFECT_DOA 4

/* Brightness is global; 48 of 255 is bright enough to read in a lit room
 * without washing out the colours on the ring. */
#define LED_BRIGHTNESS 48

static led_state_t s_current;
static bool s_initialized;

void leds_init(void) {
    s_initialized = true;
    s_current = LED_STATE_IDLE;

    /* The ring runs a rainbow, then a direction-of-arrival animation, unless
     * the host takes over. Selecting an effect explicitly stops both. */
    xvf_set_led_effect(LED_EFFECT_SINGLE);
    xvf_set_led_brightness(LED_BRIGHTNESS);
    xvf_set_led_color(0x0000FF);
}

void leds_set(led_state_t state) {
    if (s_initialized && state == s_current) {
        return;
    }
    s_initialized = true;
    s_current = state;

    switch (state) {
        case LED_STATE_OFF:
            xvf_set_led_effect(LED_EFFECT_OFF);
            return;
        case LED_STATE_IDLE:
            xvf_set_led_effect(LED_EFFECT_SINGLE);
            xvf_set_led_color(0x0000FF);
            break;
        case LED_STATE_LISTENING:
            xvf_set_led_effect(LED_EFFECT_BREATH);
            xvf_set_led_color(0x00FF00);
            break;
        case LED_STATE_THINKING:
            xvf_set_led_effect(LED_EFFECT_BREATH);
            xvf_set_led_color(0xFF8000);
            break;
        case LED_STATE_SPEAKING:
            xvf_set_led_effect(LED_EFFECT_SINGLE);
            xvf_set_led_color(0x00FFFF);
            break;
        case LED_STATE_ERROR:
            xvf_set_led_effect(LED_EFFECT_BREATH);
            xvf_set_led_color(0xFF0000);
            break;
        case LED_STATE_MUTED:
            xvf_set_led_effect(LED_EFFECT_SINGLE);
            xvf_set_led_color(0xFF0000);
            break;
        default:
            ESP_LOGW(TAG, "unknown led state %d", (int)state);
            return;
    }
    xvf_set_led_brightness(LED_BRIGHTNESS);
}
