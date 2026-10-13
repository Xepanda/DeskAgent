# CMakeLists.txt for TuyaOpen / ESP-IDF component desk_hardware
idf_component_register(
    SRCS
        "src/desk_hardware.c"
        "src/desk_arbiter.c"
    INCLUDE_DIRS
        "include"
    REQUIRES
        driver
        esp_timer
)
