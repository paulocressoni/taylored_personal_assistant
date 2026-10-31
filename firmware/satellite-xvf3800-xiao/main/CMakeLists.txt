# Register the "main" component: the code that belongs to this project.
# ESP-IDF compiles every file listed in SRCS and links it into the app.
#
# REQUIRES      = dependencies whose headers are also used by our public headers
# PRIV_REQUIRES = dependencies used only inside our .c files
idf_component_register(
    SRCS
        "app_main.c"
        "wifi.c"
        "xvf_control.c"
        "leds.c"
        "audio_i2s.c"
        "ws_client.c"
        "satellite.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES
        esp_common
        log
        nvs_flash
        esp_wifi
        esp_event
        esp_netif
        esp_timer
        esp_driver_i2c
        esp_driver_gpio
        esp_driver_i2s
        esp_websocket_client
        json
)
