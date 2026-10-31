#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/**
 * The identity this device reports to the server.
 *
 * Derived from the Wi-Fi MAC address, so it survives a reflash and stays
 * stable across reboots. The server stores it with each turn.
 *
 * @param out Buffer to fill with a NUL-terminated id.
 * @param len Capacity of `out`.
 */
void satellite_device_id(char *out, size_t len);

/** Start the supervisor task that owns the socket lifecycle. */
void satellite_start(void);

/** Report the session state: the server has acknowledged the start frame. */
bool satellite_is_ready(void);

/* Called by the WebSocket client, from its own task. */
void satellite_on_ws_connected(void);
void satellite_on_ws_text(const char *json, size_t len);
void satellite_on_ws_down(void);
