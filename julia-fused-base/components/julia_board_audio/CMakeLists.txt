idf_component_register(
    SRCS "board_audio.c"
    INCLUDE_DIRS "include"
    PRIV_REQUIRES driver esp_driver_i2s esp_driver_gpio freertos
)
