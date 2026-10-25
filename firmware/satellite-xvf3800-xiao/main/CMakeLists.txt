# Register the "main" component: the code that belongs to this project.
# ESP-IDF compiles every file listed in SRCS and links it into the app.
idf_component_register(
    SRCS "app_main.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES esp_common log
)
