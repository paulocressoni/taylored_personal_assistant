#pragma once

/**
 * The states the LED ring can show.
 *
 * The firmware maps each state to an effect and a colour; the ring itself is
 * driven by the XVF3800, not by an ESP32 GPIO.
 */
typedef enum {
    LED_STATE_OFF = 0,
    LED_STATE_IDLE,
    LED_STATE_LISTENING,
    LED_STATE_THINKING,
    LED_STATE_SPEAKING,
    LED_STATE_ERROR,
    LED_STATE_MUTED,
} led_state_t;

/** Draw the idle state once at boot. */
void leds_init(void);

/**
 * Show a state. Repeated calls with the same state do nothing, so this is safe
 * to call from a periodic loop.
 */
void leds_set(led_state_t state);
