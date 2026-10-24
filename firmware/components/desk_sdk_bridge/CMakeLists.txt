# CMakeLists.txt for TuyaOpen / ESP-IDF component desk_sdk_bridge
idf_component_register(
    SRCS
        "src/desk_sdk_bridge.c"
        "src/desk_sdk_websocket.c"
    INCLUDE_DIRS
        "include"
    REQUIRES
        desk_hardware
        desk_tclaw
        lwip
        mbedtls
        pthread
)
