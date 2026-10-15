# CMakeLists.txt for ESP-IDF main component
idf_component_register(
    SRCS "src/app_main.c"
    INCLUDE_DIRS "include"
    REQUIRES desk_hardware desk_tclaw desk_sdk_bridge
)
